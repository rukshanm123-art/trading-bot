"""CLI: status, stop/resume, approve, reports, audit — against a temp project."""

import shutil
from datetime import timedelta

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import T0, make_trend_rows, write_rows_csv
from trading_bot.cli.main import main
from trading_bot.config import constants as C
from trading_bot.core.enums import Mode
from trading_bot.core.types import dec
from trading_bot.storage.db import Database
from trading_bot.storage.repositories import Repositories

pytestmark = pytest.mark.integration


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A self-contained project dir (migrations + config + fixture data)."""
    shutil.copytree(MIGRATIONS, tmp_path / "migrations")
    fixture = write_rows_csv(make_trend_rows([(80, 0.1)], start_price=100.0), tmp_path / "fix.csv")
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
    return str(cfg_path)


def test_version(project, capsys):
    assert main(["--config", project, "version"]) == 0
    assert capsys.readouterr().out.strip()


def test_status_on_fresh_project(project, capsys):
    assert main(["--config", project, "status"]) == 0
    out = capsys.readouterr().out
    assert "PAPER" in out
    assert "Kill switch:         inactive" in out
    assert "paper simulator" in out


def test_stop_and_resume_roundtrip(project, tmp_path, capsys):
    assert main(["--config", project, "stop", "--reason", "cli test"]) == 0
    assert (tmp_path / C.STOP_FILE_NAME).exists()
    out = capsys.readouterr().out
    assert "ACTIVATED" in out

    assert main(["--config", project, "status"]) == 0
    assert "ACTIVE" in capsys.readouterr().out

    assert main(["--config", project, "resume", "--note", "resolved"]) == 0
    assert not (tmp_path / C.STOP_FILE_NAME).exists()
    assert main(["--config", project, "status"]) == 0
    assert "inactive" in capsys.readouterr().out


def test_pause_and_approve(project, capsys):
    assert main(["--config", project, "pause"]) == 0
    assert main(["--config", project, "approve", "--hours", "6"]) == 0
    out = capsys.readouterr().out
    assert "approved until" in out


def test_short_paper_run_then_report_and_audit(project, tmp_path, capsys):
    assert main(["--config", project, "paper", "run", "--cycles", "40"]) == 0
    capsys.readouterr()
    assert main(["--config", project, "report", "daily"]) == 0
    out = capsys.readouterr().out
    assert "Daily Report" in out
    assert "not financial advice" in out
    assert main(["--config", project, "report", "performance"]) == 0
    assert main(["--config", project, "audit", "verify"]) == 0
    assert "audit chain OK" in capsys.readouterr().out


def test_close_position_preview_without_position(project, capsys):
    assert main(["--config", project, "close-position-preview"]) == 0
    assert "no open position" in capsys.readouterr().out


def test_live_status_reports_locked(project, capsys):
    rc = main(["--config", project, "live", "status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "LOCKED" in out
    assert "[FAIL]" in out


def test_db_backup(project, tmp_path, capsys):
    assert main(["--config", project, "db", "migrate"]) == 0
    rc = main(["--config", project, "db", "backup", "--out", str(tmp_path / "b.db")])
    assert rc == 0
    assert (tmp_path / "b.db").exists()


def test_loss_pause_requires_dedicated_ack_and_status_is_explicit(project, tmp_path, capsys):
    db = Database(f"sqlite:///{tmp_path}/cli.db")
    db.migrate(MIGRATIONS)
    repos = Repositories(db)
    for index in range(3):
        ts = T0 + timedelta(hours=index)
        position_id = repos.positions.insert_open(
            Mode.PAPER,
            "BTCUSDT",
            dec("0.00010"),
            dec("65000"),
            dec("63700"),
            f"entry-loss-{index}",
            dec("0.01"),
            ts,
        )
        if index == 2:
            repos.positions.mark_dust(
                position_id,
                dec("0.00000991"),
                dec("0.001"),
                f"exit-loss-{index}",
                dec("0.01"),
                dec("-1"),
                "strategy_exit:dust_below_exchange_minimum",
                ts + timedelta(minutes=30),
            )
        else:
            repos.positions.close(
                position_id,
                f"exit-loss-{index}",
                dec("0.01"),
                dec("-1"),
                "test",
                ts + timedelta(minutes=30),
            )
    repos.events.reconciliation(True, {"drill": "loss-pause"}, Mode.PAPER)
    db.close()

    assert main(["--config", project, "status"]) == 0
    status = capsys.readouterr().out
    assert "ACTIVE — DOES NOT CLEAR WITH TIME" in status
    assert "effective 3 | raw history 3" in status

    assert main(["--config", project, "stop", "--reason", "combined brake drill"]) == 0
    capsys.readouterr()
    assert main(["--config", project, "resume", "--note", "reviewed"]) == 1
    resume = capsys.readouterr().out
    assert "`resume` cannot clear it" in resume
    assert "acknowledge-loss-pause" in resume
    assert main(["--config", project, "status"]) == 0
    separated_status = capsys.readouterr().out
    assert "Kill switch:         inactive" in separated_status
    assert "Loss-streak brake:   ACTIVE" in separated_status

    command = [
        "--config",
        project,
        "risk",
        "acknowledge-loss-pause",
        "--note",
        "loss sequence reviewed; continue testnet drill",
    ]
    assert main(command) == 0
    assert "effective streak is now 0" in capsys.readouterr().out
    assert main(command) == 0
    assert "idempotent retry" in capsys.readouterr().out

    check = Database(f"sqlite:///{tmp_path}/cli.db")
    audit_rows = check.query(
        "SELECT * FROM audit_log WHERE kind = 'risk.consecutive_loss_pause_acknowledged'"
    )
    acknowledgements = check.query("SELECT * FROM consecutive_loss_acknowledgements")
    assert len(audit_rows) == 1
    assert len(acknowledgements) == 1
    check.close()
