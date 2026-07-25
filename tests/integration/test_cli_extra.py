"""Additional CLI coverage: quality run/verify, live status/unlock refusal,
report performance with trades, close-position-preview with a position."""

import shutil

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_trend_rows, write_rows_csv
from trading_bot.cli.main import main
from trading_bot.core.enums import Mode
from trading_bot.core.types import dec
from trading_bot.storage.db import Database
from trading_bot.storage.repositories import Repositories

pytestmark = pytest.mark.integration


@pytest.fixture
def project(tmp_path, monkeypatch):
    shutil.copytree(MIGRATIONS, tmp_path / "migrations")
    fixture = write_rows_csv(
        make_trend_rows([(60, 0.0), (30, 1.2), (40, -0.2)], start_price=100.0),
        tmp_path / "fix.csv",
    )
    cfg = f"""
mode: paper
symbol: BTCUSDT
interval: 1h
timezone: Pacific/Auckland
db: {{ url: "sqlite:///{tmp_path}/cli.db" }}
data: {{ source: fixture, fixture_path: "{fixture}" }}
notifications: {{ console: false, email: {{ enabled: false }}, telegram: {{ enabled: false }} }}
monitoring: {{ enabled: false }}
reporting: {{ daily_time_local: "17:00", output_dir: "{tmp_path}/reports" }}
"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(cfg, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return str(cfg_path), tmp_path


def test_quality_verify_missing_record_fails(project, capsys):
    cfg_path, _ = project
    # `quality verify` on a missing record must fail cleanly (exit 1)
    rc = main(["--config", cfg_path, "quality", "verify"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "quality" in out.lower()


def test_live_status_and_unlock_refused(project, capsys):
    cfg_path, _ = project
    rc = main(["--config", cfg_path, "live", "status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "LOCKED" in out

    # unlock is refused (prerequisites unmet) without ever prompting
    rc = main(["--config", cfg_path, "live", "unlock"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "refused" in out.lower() or "FAIL" in out


def test_report_performance_with_trades(project, capsys):
    cfg_path, _ = project
    assert main(["--config", cfg_path, "paper", "run", "--cycles", "140"]) == 0
    capsys.readouterr()
    assert main(["--config", cfg_path, "report", "performance"]) == 0
    out = capsys.readouterr().out
    assert "performance" in out.lower()
    assert "do not guarantee" in out.lower()


def test_close_position_preview_with_open_position(project, capsys, tmp_path):
    cfg_path, tmp = project
    # inject an open position directly, then preview its close
    db = Database(f"sqlite:///{tmp}/cli.db")
    db.migrate(MIGRATIONS)
    repos = Repositories(db)
    repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("0.05"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-clipreview0000000000000d",
        entry_fee=dec("0.005"),
    )
    # a balance snapshot gives the preview a mark price
    repos.balances.snapshot(
        Mode.PAPER,
        {"USDT": {"free": "20", "locked": "0"}, "BTC": {"free": "0.05", "locked": "0"}},
        dec("25"),
        dec("100"),
    )
    db.close()

    rc = main(["--config", cfg_path, "close-position-preview"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PREVIEW" in out
    assert "no order will be placed" in out


def test_db_backup_and_migrate(project, capsys, tmp_path):
    cfg_path, tmp = project
    assert main(["--config", cfg_path, "db", "migrate"]) == 0
    assert "migrations" in capsys.readouterr().out.lower()
    out_db = tmp / "backup.db"
    assert main(["--config", cfg_path, "db", "backup", "--out", str(out_db)]) == 0
    assert out_db.exists()


def test_pause_then_status_shows_paused(project, capsys):
    cfg_path, _ = project
    assert main(["--config", cfg_path, "pause"]) == 0
    capsys.readouterr()
    assert main(["--config", cfg_path, "status"]) == 0
    assert "Paused flag:         True" in capsys.readouterr().out


def test_notify_test_without_external_channel_fails_cleanly(project, capsys):
    """`notify test` on a console-only config exits 1 and says why, rather
    than pretending an alert channel exists."""
    cfg_path, _ = project  # telegram/email disabled in this fixture
    rc = main(["--config", cfg_path, "notify", "test"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no external channel enabled" in out


def test_notify_test_reports_channel_delivery(project, capsys, monkeypatch, tmp_path):
    """With Telegram enabled but the network call stubbed, the command reports
    per channel and succeeds when the channel accepts the message."""
    cfg_path, _ = project
    written = tmp_path / "config.yaml"
    written.write_text(
        written.read_text(encoding="utf-8").replace(
            "telegram: { enabled: false }", "telegram: { enabled: true }"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-0123456789")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    import trading_bot.notifications.adapters as adapters

    monkeypatch.setattr(
        adapters.TelegramNotifier, "send", lambda self, subj, body, severity="info": True
    )
    rc = main(["--config", cfg_path, "notify", "test"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[SENT] telegram" in out
    assert "test alert delivered" in out
