"""Walk-forward backtest coverage + quality verifier staleness/hash branches."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import make_config
from trading_bot.backtest.engine import walk_forward
from trading_bot.backtest.synth import generate_rows, write_csv

pytestmark = pytest.mark.integration


def test_walk_forward_selects_from_validation_not_test(tmp_path):
    rows = generate_rows(n=800, seed=31)
    path = tmp_path / "wf.csv"
    write_csv(rows, path)
    result = walk_forward(make_config(), path, work_dir=tmp_path / "wf_work")
    assert result["method"].startswith("walk-forward")
    assert len(result["train"]) == 5
    assert result["out_of_sample_test"]["label"].startswith("out-of-sample")
    chosen = (result["selected_params"]["fast"], result["selected_params"]["slow"])
    val_params = {(r["strategy"]["fast"], r["strategy"]["slow"]) for r in result["validation"]}
    assert chosen in val_params
    assert "overstate" in result["honesty_note"]


def test_quality_verifier_staleness_and_hash_branches(tmp_path):
    from trading_bot.security.quality import expected_hashes, sha256_file, verify_quality_record

    qd = tmp_path / "var" / "quality"
    qd.mkdir(parents=True)
    junit = qd / "junit.xml"
    junit.write_text('<testsuite tests="150" failures="0" errors="0" skipped="0"/>', "utf-8")
    (qd / "coverage.json").write_text(json.dumps({"totals": {"percent_covered": 95.0}}), "utf-8")

    base = {
        "passed": True,
        "tests_collected": 150,
        "tests_run": 150,
        "tests_passed": 150,
        "tests_failed": 0,
        "tests_skipped": 0,
        "coverage_percent": 95.0,
        "required_safety_tests_missing": [],
        "results_hash": sha256_file(junit),
        "formatter": {"rc": 0},
        "linter": {"rc": 0},
        "type_check": {"rc": 0},
        "security_scan": {"rc": 0},
        "git_state": "no_repo",
        "git_commit": None,
        "git_dirty": False,
        **expected_hashes(tmp_path),
    }

    # stale run
    stale = {**base, "ran_at": (datetime.now(UTC) - timedelta(hours=200)).isoformat()}
    p = qd / "r.json"
    p.write_text(json.dumps(stale), "utf-8")
    res = verify_quality_record(tmp_path, p)
    assert not res.ok and any("stale" in f for f in res.failures)

    # wrong results hash
    bad_hash = {**base, "ran_at": datetime.now(UTC).isoformat(), "results_hash": "deadbeef"}
    p.write_text(json.dumps(bad_hash), "utf-8")
    res = verify_quality_record(tmp_path, p)
    assert not res.ok and any("results_hash" in f for f in res.failures)

    # require_repo but no_repo
    ok_record = {**base, "ran_at": datetime.now(UTC).isoformat()}
    p.write_text(json.dumps(ok_record), "utf-8")
    res = verify_quality_record(tmp_path, p, require_repo=True)
    assert not res.ok and any("git repository is required" in f for f in res.failures)


def test_quality_verifier_unreadable_record(tmp_path):
    from trading_bot.security.quality import verify_quality_record

    p = tmp_path / "nope.json"
    res = verify_quality_record(tmp_path, p)
    assert not res.ok
    assert any("unreadable" in f for f in res.failures)
