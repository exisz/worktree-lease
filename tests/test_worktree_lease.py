"""Behavioural tests against real temporary Git repositories. No mocks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from worktree_lease import cli  # noqa: E402


ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


OLD_DATE = "2001-01-01T00:00:00Z"


def sh(*args, cwd, date=None):
    env = dict(ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        args, cwd=str(cwd), env=env, text=True, capture_output=True, check=True
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture()
def repo(tmp_path):
    """An 'origin' bare repo plus a clone with origin/main and origin/HEAD set."""
    origin = tmp_path / "origin.git"
    sh("git", "init", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    work = tmp_path / "work"
    sh("git", "clone", str(origin), str(work), cwd=tmp_path)
    write(work / "README.md", "hello\n")
    sh("git", "add", "README.md", cwd=work)
    sh("git", "commit", "-m", "initial", cwd=work, date=OLD_DATE)
    sh("git", "push", "-u", "origin", "main", cwd=work)
    sh("git", "remote", "set-head", "origin", "main", cwd=work)
    return work


# --------------------------------------------------------------------------
# ahead_commits / carries_work
# --------------------------------------------------------------------------


def test_clean_and_not_ahead_is_not_carrying_work(repo):
    assert cli.is_clean(repo) is True
    assert cli.ahead_commits(repo) == 0
    assert cli.carries_work(repo) is False


def test_unpushed_commit_is_ahead_and_carries_work(repo):
    write(repo / "feature.txt", "work in progress\n")
    sh("git", "add", "feature.txt", cwd=repo)
    sh("git", "commit", "-m", "unpushed work", cwd=repo)

    assert cli.is_clean(repo) is True, "committed work leaves a clean tree"
    assert cli.ahead_commits(repo) == 1
    assert cli.carries_work(repo) is True


def test_dirty_worktree_carries_work(repo):
    write(repo / "scratch.txt", "uncommitted\n")
    assert cli.is_clean(repo) is False
    assert cli.carries_work(repo) is True


def test_ahead_commits_undeterminable_returns_minus_one(tmp_path):
    """A repo with no origin/* refs cannot prove delivery."""
    solo = tmp_path / "solo"
    sh("git", "init", "-b", "main", str(solo), cwd=tmp_path)
    write(solo / "a.txt", "a\n")
    sh("git", "add", "a.txt", cwd=solo)
    sh("git", "commit", "-m", "only local", cwd=solo, date=OLD_DATE)

    assert cli.ahead_commits(solo) == -1
    assert cli.carries_work(solo) is True


def test_ahead_commits_missing_path_returns_minus_one(tmp_path):
    assert cli.ahead_commits(tmp_path / "does-not-exist") == -1


# --------------------------------------------------------------------------
# reclaimable — the data-loss regression surface
# --------------------------------------------------------------------------


def expired_record(path, status="active"):
    return {
        "id": "t-1",
        "worktree": str(path),
        "thread": "t",
        "status": status,
        "claimedAt": "2000-01-01T00:00:00Z",
        "lastActiveAt": "2000-01-01T00:00:00Z",
        "ttlSeconds": 1,
    }


def test_clean_not_ahead_expired_is_reclaimable(repo):
    item = expired_record(repo)
    assert cli.reclaimable(item, repo, time.time(), 1) is True


def test_clean_but_unpushed_commits_is_never_reclaimable(repo):
    """REGRESSION: clean != delivered. Recycling this destroys commits."""
    write(repo / "feature.txt", "precious\n")
    sh("git", "add", "feature.txt", cwd=repo)
    sh("git", "commit", "-m", "precious unpushed work", cwd=repo)

    item = expired_record(repo)
    assert cli.reclaimable(item, repo, time.time(), 1) is False

    # Even an explicitly released record must not be recycled while ahead.
    released = expired_record(repo, status="released")
    assert cli.reclaimable(released, repo, time.time(), 1) is False


def test_dirty_is_never_reclaimable(repo):
    write(repo / "scratch.txt", "uncommitted\n")
    item = expired_record(repo)
    assert cli.reclaimable(item, repo, time.time(), 1) is False


def test_undeterminable_ahead_is_never_reclaimable(tmp_path):
    solo = tmp_path / "solo"
    sh("git", "init", "-b", "main", str(solo), cwd=tmp_path)
    write(solo / "a.txt", "a\n")
    sh("git", "add", "a.txt", cwd=solo)
    sh("git", "commit", "-m", "only local", cwd=solo, date=OLD_DATE)

    assert cli.ahead_commits(solo) == -1
    item = expired_record(solo, status="released")
    assert cli.reclaimable(item, solo, time.time(), 1) is False


def test_missing_path_is_not_reclaimable(tmp_path):
    ghost = tmp_path / "ghost"
    item = expired_record(ghost, status="released")
    assert cli.reclaimable(item, ghost, time.time(), 1) is False


# --------------------------------------------------------------------------
# reap_settled
# --------------------------------------------------------------------------


def test_reap_settled_demotes_settled_lost_record(repo, tmp_path):
    common = tmp_path / "leasedir"
    common.mkdir()

    item = expired_record(repo, status="lost")
    changed = cli.reap_settled([item], common)

    assert len(changed) == 1
    assert item["status"] == "released"
    assert "nothing to salvage" in item["settledReason"]

    persisted = json.loads((common / "worktree-leases" / "t-1.json").read_text())
    assert persisted["status"] == "released"


def test_reap_settled_keeps_lost_record_that_still_carries_work(repo, tmp_path):
    common = tmp_path / "leasedir"
    common.mkdir()

    write(repo / "feature.txt", "precious\n")
    sh("git", "add", "feature.txt", cwd=repo)
    sh("git", "commit", "-m", "precious unpushed work", cwd=repo)

    item = expired_record(repo, status="lost")
    changed = cli.reap_settled([item], common)

    assert changed == []
    assert item["status"] == "lost"


def test_reap_settled_releases_vanished_worktree(tmp_path):
    common = tmp_path / "leasedir"
    common.mkdir()

    item = expired_record(tmp_path / "gone", status="lost")
    changed = cli.reap_settled([item], common)

    assert len(changed) == 1
    assert item["status"] == "released"
    assert "no longer exists" in item["settledReason"]


def test_reap_settled_ignores_non_lost_records(repo, tmp_path):
    common = tmp_path / "leasedir"
    common.mkdir()

    item = expired_record(repo, status="active")
    assert cli.reap_settled([item], common) == []
    assert item["status"] == "active"


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def run_cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "worktree_lease", *args],
        cwd=str(cwd),
        env={**ENV, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        text=True,
        capture_output=True,
    )


def test_cli_help_and_version(repo):
    assert run_cli("--help", cwd=repo).returncode == 0
    v = run_cli("--version", cwd=repo)
    assert v.returncode == 0
    assert cli.__version__ in v.stdout


def test_status_json_reports_ahead_commits(repo, tmp_path):
    root, common = cli.repo_info(repo)
    cli.save(common, expired_record(repo))

    result = run_cli("status", "--json", cwd=repo)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["aheadCommits"] == 0
    assert data[0]["effectiveStatus"] == "free"


def test_status_json_marks_undelivered_expired_lease_lost(repo):
    write(repo / "feature.txt", "precious\n")
    sh("git", "add", "feature.txt", cwd=repo)
    sh("git", "commit", "-m", "precious unpushed work", cwd=repo)

    root, common = cli.repo_info(repo)
    cli.save(common, expired_record(repo))

    result = run_cli("status", "--json", cwd=repo)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data[0]["aheadCommits"] == 1
    assert data[0]["effectiveStatus"] == "lost"


def test_release_refuses_dirty_worktree(repo):
    root, common = cli.repo_info(repo)
    cli.save(common, expired_record(repo))
    write(repo / "scratch.txt", "uncommitted\n")

    result = run_cli("release", cwd=repo)
    assert result.returncode != 0
    assert "dirty" in result.stderr


def test_release_refuses_undelivered_commits_without_attestation(repo):
    root, common = cli.repo_info(repo)
    cli.save(common, expired_record(repo))
    write(repo / "feature.txt", "precious\n")
    sh("git", "add", "feature.txt", cwd=repo)
    sh("git", "commit", "-m", "precious unpushed work", cwd=repo)

    result = run_cli("release", cwd=repo)
    assert result.returncode != 0
    assert "--delivery-verified" in result.stderr


def test_release_allows_clean_and_not_ahead_without_attestation(repo):
    root, common = cli.repo_info(repo)
    cli.save(common, expired_record(repo))

    result = run_cli("release", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"

    persisted = cli.current_record(root, common)
    assert persisted["status"] == "released"
    assert "nothing to deliver" in persisted["settledReason"]


# --------------------------------------------------------------------------
# primary worktree freshness (remote-first drift)
# --------------------------------------------------------------------------


def advance_origin(repo, tmp_path, name="other"):
    """Push a new commit to origin from a second clone; returns its sha."""
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo), env=ENV, text=True, capture_output=True, check=True,
    ).stdout.strip()
    other = tmp_path / name
    sh("git", "clone", origin, str(other), cwd=tmp_path)
    write(other / f"{name}.txt", "remote work\n")
    sh("git", "add", f"{name}.txt", cwd=other)
    sh("git", "commit", "-m", f"{name} remote commit", cwd=other)
    sh("git", "push", "origin", "main", cwd=other)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(other), env=ENV, text=True, capture_output=True, check=True,
    ).stdout.strip()


def head_of(path, rev="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", rev],
        cwd=str(path), env=ENV, text=True, capture_output=True, check=True,
    ).stdout.strip()


def test_claim_fast_forwards_stale_primary_and_branches_from_remote_tip(repo, tmp_path):
    remote_sha = advance_origin(repo, tmp_path)
    assert head_of(repo) != remote_sha

    result = run_cli("claim", "--thread", "t-ff", cwd=repo)
    assert result.returncode == 0, result.stderr

    # Primary was fast-forwarded rather than left behind forever.
    assert head_of(repo) == remote_sha
    # And the issued worktree is based on the freshest remote tip.
    issued = Path(result.stdout.strip())
    assert issued.exists()
    assert head_of(issued) == remote_sha


def test_status_warns_when_primary_has_diverged(repo, tmp_path):
    advance_origin(repo, tmp_path)
    write(repo / "local.txt", "local only\n")
    sh("git", "add", "local.txt", cwd=repo)
    sh("git", "commit", "-m", "local only commit", cwd=repo)
    sh("git", "fetch", "origin", cwd=repo)

    result = run_cli("status", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "DIVERGED_PRIMARY" in result.stderr
    assert "ahead" in result.stderr and "behind" in result.stderr


def test_status_warns_when_primary_is_merely_behind(repo, tmp_path):
    advance_origin(repo, tmp_path)
    sh("git", "fetch", "origin", cwd=repo)

    result = run_cli("status", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert "DIVERGED_PRIMARY" in result.stderr
    assert "behind" in result.stderr


def test_sync_primary_refuses_to_merge_a_diverged_primary(repo, tmp_path):
    advance_origin(repo, tmp_path)
    write(repo / "local.txt", "local only\n")
    sh("git", "add", "local.txt", cwd=repo)
    sh("git", "commit", "-m", "local only commit", cwd=repo)
    sh("git", "fetch", "origin", cwd=repo)
    before = head_of(repo)

    root, common = cli.repo_info(repo)
    cli.sync_primary(common, "origin", "main")

    # Divergence must never be silently merged away.
    assert head_of(repo) == before


def test_sync_primary_skips_dirty_primary(repo, tmp_path):
    remote_sha = advance_origin(repo, tmp_path)
    write(repo / "scratch.txt", "uncommitted\n")
    sh("git", "fetch", "origin", cwd=repo)
    before = head_of(repo)

    root, common = cli.repo_info(repo)
    cli.sync_primary(common, "origin", "main")

    assert head_of(repo) == before != remote_sha


def test_release_fast_forwards_primary_even_with_nothing_to_deliver(repo, tmp_path):
    """Work can reach the delivery branch by other means; the primary must not
    be left stale just because this worktree had nothing of its own to push."""
    root, common = cli.repo_info(repo)
    cli.save(common, expired_record(repo))
    remote_sha = advance_origin(repo, tmp_path, name="elsewhere")
    assert head_of(repo) != remote_sha

    result = run_cli("release", "--target", "main", cwd=repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"
    assert head_of(repo) == remote_sha


def test_repo_policy_denies_primary_and_existing_linked_checkout(repo, tmp_path):
    linked = tmp_path / "linked"
    sh("git", "worktree", "add", "-b", "older", str(linked), cwd=repo)
    write(repo / ".worktreerc", "# Instance state directory\nallow_worktree: false\n")
    for path in (repo, linked):
        root, common = cli.repo_info(path)
        with pytest.raises(SystemExit, match="allow_worktree: false"):
            cli.reject_repository_policy(root, common)


def test_repo_policy_survives_clone(repo, tmp_path):
    write(repo / ".worktreerc", "allow_worktree: false\n")
    sh("git", "add", ".worktreerc", cwd=repo)
    sh("git", "commit", "-m", "policy", cwd=repo)
    clone = tmp_path / "moved"
    sh("git", "clone", str(repo), str(clone), cwd=tmp_path)
    with pytest.raises(SystemExit, match="allow_worktree: false"):
        cli.reject_repository_policy(*cli.repo_info(clone))


def test_repo_policy_invalid_fails_closed_and_true_allows(repo):
    root, common = cli.repo_info(repo)
    cli.reject_repository_policy(root, common)
    write(repo / ".worktreerc", "allow_worktree: true\n")
    cli.reject_repository_policy(root, common)
    write(repo / ".worktreerc", "allow_worktree: flase\n")
    with pytest.raises(SystemExit, match="invalid repository policy"):
        cli.reject_repository_policy(root, common)


def test_nested_default_is_clean_and_resolves_primary(repo):
    exclude = repo / '.git/info/exclude'
    exclude.write_text('# preserved without newline')
    first = run_cli('claim', '--thread', 'nested-first', cwd=repo)
    assert first.returncode == 0, first.stderr
    path = Path(first.stdout.strip())
    assert path.parent == repo / '.worktrees'
    assert cli.is_clean(repo)
    second = run_cli('claim', '--thread', 'nested-second', cwd=path)
    assert second.returncode == 0, second.stderr
    assert Path(second.stdout.strip()).parent == repo / '.worktrees'
    assert exclude.read_text() == '# preserved without newline\n/.worktrees/\n'
    assert cli.is_clean(repo)


def test_legacy_active_lease_and_explicit_override_remain_supported(repo, tmp_path):
    override = tmp_path / 'legacy'
    first = run_cli('claim', '--thread', 'legacy-thread', '--worktrees-dir', str(override), cwd=repo)
    assert first.returncode == 0, first.stderr
    legacy = Path(first.stdout.strip())
    assert legacy.parent == override / repo.name
    again = run_cli('claim', '--thread', 'legacy-thread', cwd=repo)
    assert again.returncode == 0, again.stderr
    assert Path(again.stdout.strip()) == legacy
    assert run_cli('touch', cwd=legacy).returncode == 0
    released = run_cli('release', cwd=legacy)
    assert released.returncode == 0, released.stderr
    fresh = run_cli('claim', '--thread', 'fresh-thread', cwd=repo)
    assert fresh.returncode == 0, fresh.stderr
    assert Path(fresh.stdout.strip()).parent == repo / '.worktrees'


def test_nested_default_in_absorbed_submodule(repo, tmp_path):
    parent = tmp_path / 'parent'
    sh('git', 'init', '-b', 'main', str(parent), cwd=tmp_path)
    sh('git', '-c', 'protocol.file.allow=always', 'submodule', 'add', str(repo), 'repos/child', cwd=parent)
    sh('git', 'commit', '-am', 'add child', cwd=parent)
    child = parent / 'repos/child'
    claimed = run_cli('claim', '--thread', 'child-thread', cwd=child)
    assert claimed.returncode == 0, claimed.stderr
    assert Path(claimed.stdout.strip()).parent == child / '.worktrees'
    assert cli.is_clean(child)
    assert cli.is_clean(parent)
