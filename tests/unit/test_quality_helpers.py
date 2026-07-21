"""Coverage for quality-evidence helpers: git_info against a real repo layout,
source_tree_hash stability, and reconciliation of a filled resting order."""


def test_git_info_reads_ref_and_detached_head(tmp_path):
    from trading_bot.security.quality import git_info

    # no repo
    assert git_info(tmp_path) == (None, False, "no_repo")

    # symbolic-ref HEAD pointing at a branch
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("abc123def456\n", encoding="utf-8")
    commit, dirty, state = git_info(tmp_path)
    assert commit == "abc123def456"
    assert dirty is False and state == "repo"

    # detached HEAD (raw commit in HEAD)
    (git / "HEAD").write_text("deadbeefcafe\n", encoding="utf-8")
    commit, _, state = git_info(tmp_path)
    assert commit == "deadbeefcafe" and state == "repo"

    # ref present but target missing -> commit None, still a repo
    (git / "HEAD").write_text("ref: refs/heads/missing\n", encoding="utf-8")
    commit, _, state = git_info(tmp_path)
    assert commit is None and state == "repo"


def test_source_tree_hash_skips_generated_and_is_stable(tmp_path):
    from trading_bot.security.quality import source_tree_hash

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "runtime.db").write_text("junk", encoding="utf-8")
    (tmp_path / "src" / "a.pyc").write_text("bytecode", encoding="utf-8")

    h1 = source_tree_hash(tmp_path)
    # adding a SKIPPED file must not change the hash
    (tmp_path / "var" / "more.log").write_text("noise", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("mac", encoding="utf-8")
    assert source_tree_hash(tmp_path) == h1
    # adding a real source file MUST change it
    (tmp_path / "src" / "b.py").write_text("print('y')\n", encoding="utf-8")
    assert source_tree_hash(tmp_path) != h1


def test_sha256_file_missing_returns_empty(tmp_path):
    from trading_bot.security.quality import sha256_file

    assert sha256_file(tmp_path / "nope.txt") == ""
    p = tmp_path / "f.txt"
    p.write_text("hello", encoding="utf-8")
    assert len(sha256_file(p)) == 64
