"""Quality evidence hashes are recomputed, not trusted blindly."""

import json
from datetime import UTC, datetime

from trading_bot.config import constants as C
from trading_bot.security.quality import expected_hashes, verify_quality_record


def write_quality(root, **overrides):
    quality_dir = root / "var" / "quality"
    quality_dir.mkdir(parents=True)
    (quality_dir / "junit.xml").write_text("<testsuite tests='1' failures='0' errors='0'/>")
    (quality_dir / "coverage.json").write_text('{"totals":{"percent_covered":95.0}}')
    (root / "requirements.txt").write_text("requests==2.34.2\n")
    (root / "requirements-dev.txt").write_text("-r requirements.txt\n")
    cfg_dir = root / "src" / "trading_bot" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "models.py").write_text("MODEL = 1\n")
    (cfg_dir / "constants.py").write_text("CONST = 1\n")
    payload = {
        "passed": True,
        "tests_collected": 150,
        "tests_passed": 150,
        "tests_failed": 0,
        "tests_skipped": 0,
        "coverage_percent": 95.0,
        "required_safety_tests_missing": [],
        "results_hash": "abc",
        "git_commit": None,
        "git_dirty": False,
        "git_state": "no_repo",
        "ran_at": datetime.now(UTC).isoformat(),
        **expected_hashes(root),
    }
    payload.update(overrides)
    path = root / C.QUALITY_GATE_FILE
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_quality_evidence_rejects_zero_tests(tmp_path):
    write_quality(tmp_path, tests_collected=0, tests_passed=0)
    result = verify_quality_record(tmp_path, require_repo=False)
    assert not result.ok
    assert "zero tests collected" in result.failures


def test_quality_evidence_rejects_tampered_coverage_artifact(tmp_path):
    write_quality(tmp_path)
    (tmp_path / "var" / "quality" / "coverage.json").write_text(
        '{"totals":{"percent_covered":10.0}}'
    )
    result = verify_quality_record(tmp_path, require_repo=False)
    assert not result.ok
    assert "coverage_report_hash mismatch" in result.failures


def test_quality_evidence_rejects_source_changes_after_record(tmp_path):
    write_quality(tmp_path)
    (tmp_path / "src" / "trading_bot" / "config" / "models.py").write_text("MODEL = 2\n")
    result = verify_quality_record(tmp_path, require_repo=False)
    assert not result.ok
    assert "configuration_schema_hash mismatch" in result.failures
    assert "source_tree_hash mismatch" in result.failures


def test_quality_evidence_accepts_recorded_dirty_state_without_git_shelling(tmp_path):
    path = write_quality(tmp_path, git_dirty=True)

    result = verify_quality_record(tmp_path, path, require_repo=False)

    assert "git dirty state mismatch" not in result.failures
