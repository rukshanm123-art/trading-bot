"""Daily report: built once per local-timezone day, stored in DB + var/reports/.

The report's "recommended action" is advisory text only — it cannot loosen or
override anything the risk engine enforces.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.ai.analyst import TemplateAnalyst
from trading_bot.config.models import AppConfig
from trading_bot.core.enums import Recommendation
from trading_bot.core.models import SymbolRules
from trading_bot.core.types import HUNDRED, ZERO, dec
from trading_bot.exchange.interface import Clock
from trading_bot.notifications.adapters import NotificationHub
from trading_bot.reporting.performance import benchmark_comparison, trade_stats
from trading_bot.risk.loss_pause import ConsecutiveLossPauseService
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)


def local_day_bounds_utc(day: str, timezone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone)
    local_start = datetime.fromisoformat(day).replace(tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


class DailyReportBuilder:
    def __init__(
        self,
        repos: Repositories,
        cfg: AppConfig,
        rules: SymbolRules,
        clock: Clock,
        hub: NotificationHub | None = None,
    ) -> None:
        self.repos = repos
        self.cfg = cfg
        self.rules = rules
        self.clock = clock
        self.hub = hub
        self.analyst = TemplateAnalyst()

    # ------------------------------------------------------------------
    def local_day(self, ts: datetime | None = None) -> str:
        tz = ZoneInfo(self.cfg.timezone)
        return (ts or self.clock.now()).astimezone(tz).strftime("%Y-%m-%d")

    def build(
        self,
        day: str,
        health: dict[str, Any],
        kill_switch_active: bool,
        kill_switch_reason: str,
        trading_permitted: bool,
    ) -> tuple[dict[str, Any], str]:
        mode = self.cfg.mode
        utc_start, utc_end = local_day_bounds_utc(day, self.cfg.timezone)
        de = self.repos.daily_equity.get(day, mode)
        latest = self.repos.balances.latest_at_or_before(
            mode, utc_end
        ) or self.repos.balances.latest(mode)
        first = self.repos.balances.first(mode)

        equity_now = dec(str(latest["equity"])) if latest else ZERO
        price_now = dec(str(latest["quote_price"])) if latest and latest["quote_price"] else None
        start_snapshot = self.repos.balances.latest_at_or_before(mode, utc_start)
        start_equity = (
            dec(str(de["start_equity"]))
            if de
            else dec(str(start_snapshot["equity"]))
            if start_snapshot
            else equity_now
        )

        closed = self.repos.positions.closed_between(mode, utc_start, utc_end)
        opened = self.repos.positions.opened_between(mode, utc_start, utc_end)
        realizations = self.repos.positions.realizations_between(mode, utc_start, utc_end)
        realized_rows = realizations or closed
        pnls = [
            dec(str(r["realized_pnl"])) for r in realized_rows if r.get("realized_pnl") is not None
        ]
        fees_today = sum(
            (dec(str(r["exit_fee"])) for r in realized_rows if r.get("exit_fee")), ZERO
        ) + sum((dec(str(r["entry_fee"])) for r in opened), ZERO)
        realized_today = sum(pnls, ZERO)

        position = self.repos.positions.open_position(mode)
        unrealized = ZERO
        position_view: dict[str, Any] | None = None
        if position is not None and price_now is not None:
            unrealized = position.unrealized_pnl(price_now)
            position_view = {
                **position.as_dict(),
                "mark_price": str(price_now),
                "unrealized_pnl": str(unrealized),
            }

        return_pct = (
            (equity_now - start_equity) / start_equity * HUNDRED if start_equity > ZERO else ZERO
        )

        decisions = self.repos.decisions.between(mode, utc_start, utc_end)
        signal_counts: dict[str, int] = {}
        rejected_entries: dict[str, int] = {}
        for d in decisions:
            action = d.get("signal_action") or "NONE"
            signal_counts[action] = signal_counts.get(action, 0) + 1
            if d.get("signal_action") == "ENTER_LONG" and d.get("risk_approved") == 0:
                try:
                    codes = json.loads(d.get("risk_codes_json") or "[]")
                except json.JSONDecodeError:
                    codes = []
                key = codes[0] if codes else "UNSPECIFIED"
                rejected_entries[key] = rejected_entries.get(key, 0) + 1

        alerts = self.repos.events.alerts_between(utc_start, utc_end)
        api_errors = self.repos.events.api_errors_between(utc_start, utc_end)
        loss_pause = ConsecutiveLossPauseService(self.repos, self.cfg, self.clock).status()
        consecutive_losses = loss_pause.effective_streak

        peak = self.repos.balances.peak_equity(mode) or equity_now
        drawdown_pct = (peak - equity_now) / peak * HUNDRED if peak > ZERO else ZERO

        start_price = dec(str(first["quote_price"])) if first and first["quote_price"] else None
        start_equity_all = dec(str(first["equity"])) if first else equity_now
        benchmarks = benchmark_comparison(
            start_equity_all,
            equity_now,
            start_price,
            price_now,
            fee_bps=self.cfg.paper.taker_fee_bps,
        )

        recommendation = self._recommend(
            realized_today,
            start_equity,
            drawdown_pct,
            consecutive_losses,
            kill_switch_active,
            health,
        )

        report: dict[str, Any] = {
            "day": day,
            "timezone": self.cfg.timezone,
            "mode": mode.value,
            "symbol": self.cfg.symbol,
            "quote_asset": self.rules.quote_asset,
            "start_of_day_equity": str(start_equity),
            "end_of_day_equity": str(equity_now),
            "return_pct": str(return_pct.quantize(Decimal("0.0001"))),
            "realized_pnl_today": str(realized_today),
            "unrealized_pnl": str(unrealized),
            "fees_today": str(fees_today),
            "slippage_note": "paper fills include spread + bounded random slippage",
            "trades_opened_count": len(opened),
            "trades_closed_count": len(closed),
            "trade_stats_today": trade_stats(
                pnls, [dec(str(r["exit_fee"])) for r in realized_rows if r.get("exit_fee")]
            ),
            "open_position": position_view,
            "consecutive_losses": consecutive_losses,
            "raw_consecutive_losses": loss_pause.raw_streak,
            "consecutive_loss_pause": loss_pause.as_dict(),
            "current_drawdown_pct": str(drawdown_pct.quantize(Decimal("0.01"))),
            "signal_counts": signal_counts,
            "rejected_entries": rejected_entries,
            "risk_limit_events": [
                {"ts": a["ts"], "kind": a["kind"], "message": a["message"]} for a in alerts
            ],
            "api_errors_count": len(api_errors),
            "benchmarks": benchmarks,
            "kill_switch_active": kill_switch_active,
            "kill_switch_reason": kill_switch_reason,
            "trading_permitted": trading_permitted,
            "health": health,
            "recommendation": recommendation.value,
            "recommendation_note": (
                "Advisory only. The deterministic risk engine and kill switches remain "
                "authoritative regardless of this recommendation."
            ),
        }
        report["narrative"] = self.analyst.narrate_day(report)
        return report, self._render_markdown(report)

    # ------------------------------------------------------------------
    def _recommend(
        self,
        realized_today: Decimal,
        start_equity: Decimal,
        drawdown_pct: Decimal,
        consecutive_losses: int,
        kill_switch_active: bool,
        health: dict[str, Any],
    ) -> Recommendation:
        r = self.cfg.risk
        unhealthy = any(health.get(k) == "failed" for k in ("database", "exchange", "market_data"))
        if kill_switch_active or unhealthy:
            return Recommendation.INVESTIGATE
        if start_equity > ZERO:
            day_loss_pct = -realized_today / start_equity * HUNDRED
            if day_loss_pct >= r.max_daily_loss_pct:
                return Recommendation.PAUSE
        if drawdown_pct >= r.max_drawdown_pct:
            return Recommendation.PAUSE
        if consecutive_losses >= r.pause_after_consecutive_losses:
            return Recommendation.PAUSE
        if realized_today < ZERO or consecutive_losses > 0:
            return Recommendation.CONTINUE_WITH_CAUTION
        return Recommendation.CONTINUE

    # ------------------------------------------------------------------
    def _render_markdown(self, r: dict[str, Any]) -> str:
        q = r["quote_asset"]
        lines = [
            f"# Daily Report — {r['day']} ({r['timezone']})",
            "",
            f"**Mode:** {r['mode'].upper()}   **Symbol:** {r['symbol']}   "
            f"**Trading permitted:** {'yes' if r['trading_permitted'] else 'NO'}",
            "",
            "## P&L",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Start-of-day equity | {r['start_of_day_equity']} {q} |",
            f"| End-of-day equity | {r['end_of_day_equity']} {q} |",
            f"| Return | {r['return_pct']}% |",
            f"| Realised P&L | {r['realized_pnl_today']} {q} |",
            f"| Unrealised P&L | {r['unrealized_pnl']} {q} |",
            f"| Fees | {r['fees_today']} {q} |",
            f"| Current drawdown | {r['current_drawdown_pct']}% |",
            f"| Consecutive losses (effective / raw) | {r['consecutive_losses']} / {r['raw_consecutive_losses']} |",
            "",
            "## Activity",
            "",
            f"- Trades opened: {r['trades_opened_count']}, closed: {r['trades_closed_count']}",
            f"- Signals: {json.dumps(r['signal_counts'])}",
            f"- Rejected entries by reason: {json.dumps(r['rejected_entries']) or 'none'}",
            f"- API errors: {r['api_errors_count']}",
        ]
        if r["open_position"]:
            p = r["open_position"]
            lines += [
                "",
                "## Open position",
                "",
                f"- {p['qty']} {r['symbol']} @ {p['avg_entry_price']} "
                f"(stop {p['stop_price']}, mark {p['mark_price']}, "
                f"unrealised {p['unrealized_pnl']} {q})",
            ]
        else:
            lines += ["", "## Open position", "", "- none"]
        b = r["benchmarks"]
        lines += [
            "",
            "## Benchmarks (since inception)",
            "",
            "| Strategy | Buy & hold | No-trade (cash) |",
            "|---|---|---|",
            f"| {b['strategy_return_pct']}% | {b['buy_and_hold_return_pct'] or 'n/a'}% | 0% |",
        ]
        if r["risk_limit_events"]:
            lines += ["", "## Risk events", ""]
            lines += [
                f"- {e['ts']} **{e['kind']}** — {e['message']}" for e in r["risk_limit_events"]
            ]
        if r["kill_switch_active"]:
            lines += ["", f"> ⛔ **KILL SWITCH ACTIVE**: {r['kill_switch_reason']}"]
        loss_pause = r["consecutive_loss_pause"]
        if loss_pause["active"]:
            lines += [
                "",
                "> ⛔ **CONSECUTIVE-LOSS PAUSE ACTIVE — THIS DOES NOT CLEAR WITH TIME.**",
                f"> Latched since: {loss_pause['active_since']}",
                f"> Earliest operator acknowledgement: {loss_pause['minimum_ack_at']}",
                f"> Recovery command: `{ConsecutiveLossPauseService.ACK_COMMAND}`",
            ]
        if loss_pause["latest_acknowledgement"]:
            ack = loss_pause["latest_acknowledgement"]
            lines += [
                "",
                "## Last consecutive-loss review",
                "",
                f"- Acknowledged: {ack['acknowledged_at']} by {ack['actor']}",
                f"- Watermark position: {ack['watermark_position_id']}",
                f"- Review note: {ack['note']}",
            ]
        lines += [
            "",
            "## Health",
            "",
            "```json",
            json.dumps(r["health"], indent=2),
            "```",
            "",
            f"## Recommendation: **{r['recommendation'].replace('_', ' ').upper()}**",
            "",
            r["narrative"],
            "",
            f"_{r['recommendation_note']}_",
            "",
            "---",
            "_Paper/backtest results do not guarantee future performance. "
            "This report is not financial advice._",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def generate_and_store(
        self,
        day: str,
        health: dict[str, Any],
        kill_switch_active: bool,
        kill_switch_reason: str,
        trading_permitted: bool,
    ) -> dict[str, Any]:
        report, md = self.build(
            day, health, kill_switch_active, kill_switch_reason, trading_permitted
        )
        self.repos.reports.insert(day, "daily", self.cfg.mode, md, report)
        out_dir = Path(self.cfg.reporting.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"daily-{day}.md").write_text(md, encoding="utf-8")
        # roll the daily_equity row forward
        latest = self.repos.balances.latest(self.cfg.mode)
        if latest:
            self.repos.daily_equity.set_day_end(
                day,
                self.cfg.mode,
                dec(str(latest["equity"])),
                dec(report["realized_pnl_today"]),
                dec(report["fees_today"]),
                report["trades_opened_count"],
            )
        if self.hub:
            severity = "critical" if report["recommendation"] == "investigate" else "info"
            self.hub.send(
                f"Daily report {day} [{self.cfg.mode.value}] — {report['recommendation']}",
                report["narrative"],
                severity,
            )
        log.info("daily report %s stored (%s)", day, report["recommendation"])
        return report
