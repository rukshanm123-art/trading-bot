"""Static guarantees: no withdrawal capability, no eval/exec, no shell-outs,
and orders can only originate from the execution gateway."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "trading_bot"
PY_FILES = sorted(SRC.rglob("*.py"))


def read_all() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text(encoding="utf-8") for p in PY_FILES}


def test_no_withdrawal_endpoints_or_methods():
    """The withdrawal capability must not exist anywhere in the codebase.
    (Reading the 'enableWithdrawals' permission FLAG to refuse unsafe keys is
    the one allowed mention.)"""
    sources = read_all()
    for name, text in sources.items():
        assert "/sapi/v1/capital" not in text, f"withdrawal endpoint in {name}"
        assert not re.search(r"def \w*withdraw", text, re.I), f"withdraw method in {name}"
        assert not re.search(r"def \w*transfer", text, re.I), f"transfer method in {name}"
    # the permission check itself must exist (refuses withdrawal-enabled keys)
    assert "enableWithdrawals" in sources["exchange/binance.py"]


def test_no_dynamic_code_execution():
    for name, text in read_all().items():
        assert not re.search(r"\beval\(", text), f"eval() in {name}"
        assert not re.search(r"\bexec\(", text), f"exec() in {name}"
        assert "subprocess" not in text, f"subprocess in {name}"
        assert "os.system" not in text, f"os.system in {name}"


def test_no_leverage_margin_or_futures_endpoints():
    for name, text in read_all().items():
        assert "/fapi/" not in text, f"futures endpoint in {name}"
        assert "marginType" not in text, f"margin usage in {name}"
        assert "leverage=" not in text.lower(), f"leverage parameter in {name}"


def test_create_order_called_only_by_gateway_and_paper_sim():
    """adapter.create_order may be invoked only from the execution gateway.
    (paper.py DEFINES the simulator implementation; backtests go through the
    same gateway.)"""
    offenders = []
    for name, text in read_all().items():
        if name in ("execution/gateway.py",):
            continue
        for match in re.finditer(r"(\w+)\.create_order\(", text):
            if name == "exchange/paper.py" and match.group(1) == "self":
                continue
            offenders.append(f"{name}: {match.group(0)}")
    assert not offenders, f"orders submitted outside the gateway: {offenders}"


def test_no_risk_bypass_flags():
    for name, text in read_all().items():
        assert "skip_risk" not in text, f"risk bypass flag in {name}"
        assert "bypass_risk" not in text, f"risk bypass flag in {name}"
        assert "force_trade" not in text, f"risk bypass flag in {name}"


def test_gateway_mode_separation(repos):
    """A live-kind adapter can never be driven by a paper-mode gateway."""
    import pytest

    from tests.helpers import RULES, T0, FakeQuoteSource, make_config
    from trading_bot.core.enums import Mode
    from trading_bot.exchange.interface import FrozenClock
    from trading_bot.exchange.paper import PaperExchange
    from trading_bot.execution.gateway import ExecutionGateway, GatewaySecurityError
    from trading_bot.risk.engine import RiskEngine
    from trading_bot.storage.audit import AuditLog

    cfg = make_config()
    paper = PaperExchange(RULES, cfg.paper, FakeQuoteSource(), repos.sim_state, FrozenClock(T0))
    paper.kind = "live"  # simulate a miswired adapter
    with pytest.raises(GatewaySecurityError, match="cannot use adapter kind"):
        ExecutionGateway(
            paper,
            repos,
            RiskEngine(cfg, __import__("decimal").Decimal(10)),
            Mode.PAPER,
            AuditLog(repos.db),
            FrozenClock(T0),
            kill_switch_check=lambda: (False, ""),
        )
