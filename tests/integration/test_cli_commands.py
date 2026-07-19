"""CLI: status, stop/resume, approve, reports, audit — against a temp project."""

import shutil

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_trend_rows, write_rows_csv
from trading_bot.cli.main import main
from trading_bot.config import constants as C

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
