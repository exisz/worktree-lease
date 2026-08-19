"""Small, dependency-free Git worktree lease manager."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_TTL = int(os.environ.get("WORKTREE_LEASE_TTL", "86400"))


def run(args, cwd=None, check=True, capture=True):
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=capture)
    if check and p.returncode:
        msg = (p.stderr or p.stdout or "command failed").strip()
        raise SystemExit(msg)
    return p


def git(repo, *args, check=True):
    return run(["git", "-C", str(repo), *args], check=check)


def slug(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return value or "thread"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def repo_info(repo):
    root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    common_raw = git(root, "rev-parse", "--git-common-dir").stdout.strip()
    common = (root / common_raw).resolve() if not os.path.isabs(common_raw) else Path(common_raw).resolve()
    return root, common


def openclaw_config_path():
    explicit = os.environ.get("OPENCLAW_CONFIG_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    state_dir = Path(os.environ.get("OPENCLAW_STATE_DIR", "~/.openclaw")).expanduser().resolve()
    return state_dir / "openclaw.json"


def configured_agents():
    result = run(["openclaw", "agents", "list", "--json"], check=False)
    if result.returncode == 0:
        try:
            agents = json.loads(result.stdout)
            if isinstance(agents, list):
                return agents
        except json.JSONDecodeError:
            pass
    config_path = openclaw_config_path()
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot verify OpenClaw workspace roots from {config_path}: {exc}")
    return config.get("agents", {}).get("list", [])


def configured_workspace_repos():
    repos = []
    for agent in configured_agents():
        workspace = agent.get("workspace")
        if not workspace:
            continue
        path = Path(os.path.expandvars(workspace)).expanduser().resolve()
        if not path.exists():
            continue
        result = git(path, "rev-parse", "--show-toplevel", check=False)
        if result.returncode != 0:
            continue
        workspace_root = Path(result.stdout.strip()).resolve()
        common_raw = git(workspace_root, "rev-parse", "--git-common-dir").stdout.strip()
        common = (
            (workspace_root / common_raw).resolve()
            if not os.path.isabs(common_raw)
            else Path(common_raw).resolve()
        )
        repos.append((agent.get("id", "unknown"), workspace_root, common))
    return repos


def reject_openclaw_workspace_repo(root, common):
    for agent_id, workspace_root, workspace_common in configured_workspace_repos():
        if common == workspace_common:
            raise SystemExit(
                "refusing to lease an OpenClaw agent workspace repository "
                f"(agent={agent_id}, workspace={workspace_root}); "
                "worktree leases are only for project/source repositories"
            )


def lease_dir(common):
    path = common / "worktree-leases"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def mutex(common):
    lock = lease_dir(common) / ".lock"
    deadline = time.time() + 10
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    shutil.rmtree(lock)
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise SystemExit("timed out waiting for lease lock")
            time.sleep(0.05)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def records(common):
    out = []
    for path in lease_dir(common).glob("*.json"):
        try:
            item = json.loads(path.read_text())
            item["_file"] = str(path)
            out.append(item)
        except (OSError, json.JSONDecodeError):
            pass
    return out


def save(common, item):
    path = lease_dir(common) / f"{slug(item['id'])}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def current_record(root, common):
    root_s = str(root.resolve())
    for item in records(common):
        try:
            if str(Path(item["worktree"]).resolve()) == root_s:
                return item
        except KeyError:
            continue
    return None


def is_clean(path):
    return git(path, "status", "--porcelain", check=False).stdout.strip() == ""


def ahead_commits(path):
    """Commits on this worktree not yet contained in its delivery target.

    Returns -1 when the answer cannot be established, which callers must treat
    as "possibly carrying work" and therefore never auto-recyclable.
    """
    if not path.exists():
        return -1
    if git(path, "rev-parse", "HEAD", check=False).returncode != 0:
        return -1
    for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master", "refs/remotes/origin/HEAD"):
        if git(path, "rev-parse", "--verify", "--quiet", ref, check=False).returncode != 0:
            continue
        counted = git(path, "rev-list", "--count", f"{ref}..HEAD", check=False)
        if counted.returncode == 0 and counted.stdout.strip().isdigit():
            return int(counted.stdout.strip())
    return -1


def carries_work(path):
    """True when the worktree may still hold unsaved or undelivered work."""
    if not is_clean(path):
        return True
    return ahead_commits(path) != 0


def last_commit_at(path):
    result = git(path, "log", "-1", "--format=%ct", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    return int(result.stdout.strip())


def thread_id(args):
    value = args.thread or os.environ.get("OPENCLAW_SESSION_KEY") or os.environ.get("OPENCLAW_THREAD_ID")
    if not value:
        raise SystemExit("thread id required: pass --thread or set OPENCLAW_SESSION_KEY")
    return value


def reclaimable(item, path, now, ttl):
    if not path.exists() or not is_clean(path):
        return False
    # Clean is not the same as delivered: a tidy worktree can still hold commits
    # that were never pushed to the delivery target. Recycling those loses work.
    if ahead_commits(path) != 0:
        return False
    if item.get("status") == "released":
        return True
    lease_old = now - parse_time(item["lastActiveAt"]) > ttl
    commit_old = now - last_commit_at(path) > ttl
    return lease_old and commit_old


def reap_settled(items, common):
    """Demote worktrees flagged lost that no longer carry any work.

    A worktree is only genuinely lost when it is dirty or holds undelivered
    commits. Once it is clean and not ahead of the delivery target there is
    nothing left to salvage, so it returns to the reusable pool instead of
    accumulating as a permanent zombie demanding human review.
    """
    changed = []
    for item in items:
        if item.get("status") != "lost":
            continue
        path = Path(item.get("worktree", ""))
        if not path.exists():
            item["status"] = "released"
            item["settledReason"] = "worktree path no longer exists"
            item["settledAt"] = now_iso()
            save(common, item)
            changed.append(item)
            continue
        if not carries_work(path):
            item["status"] = "released"
            item["settledReason"] = "clean and not ahead of delivery target; nothing to salvage"
            item["settledAt"] = now_iso()
            save(common, item)
            changed.append(item)
    return changed


def warn_lost(items):
    lost = [item for item in items if item.get("status") == "lost"]
    if not lost:
        return
    print(
        f"LOST_WORKTREE: found {len(lost)} abandoned dirty worktree(s); "
        "current claim will continue. Tell the user and ask whether to investigate:",
        file=sys.stderr,
    )
    for item in lost:
        print(f"  thread={item.get('thread', 'legacy')} path={item.get('worktree')}", file=sys.stderr)


def base_ref(repo):
    origin_head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if origin_head.returncode == 0 and origin_head.stdout.strip():
        return origin_head.stdout.strip()
    branch = git(repo, "branch", "--show-current", check=False).stdout.strip()
    return branch or "HEAD"


def worktrees_root(repo, common, explicit_root=None):
    if explicit_root:
        return Path(explicit_root).expanduser().resolve() / slug(repo.name)
    primary_repo = common.parent if common.name == ".git" else repo
    return primary_repo.parent / f"{slug(primary_repo.name)}-worktrees"


def cmd_claim(args):
    repo, common = repo_info(args.repo)
    reject_openclaw_workspace_repo(repo, common)
    thread = thread_id(args)
    thread_slug = slug(thread)
    ttl = args.ttl
    home = worktrees_root(repo, common, args.worktrees_dir)
    with mutex(common):
        items = records(common)
        now = time.time()
        for item in items:
            path = Path(item.get("worktree", ""))
            item_ttl = item.get("ttlSeconds", ttl)
            lease_old = now - parse_time(item["lastActiveAt"]) > item_ttl
            if item.get("status") in {"active", "stale-dirty"} and lease_old and path.exists() and carries_work(path):
                item["status"] = "lost"
                save(common, item)
        reap_settled(items, common)
        for item in items:
            if item.get("thread") == thread and item.get("status") == "active":
                path = Path(item["worktree"])
                if path.exists():
                    item["lastActiveAt"] = now_iso()
                    item["status"] = "active"
                    save(common, item)
                    warn_lost(items)
                    print(path)
                    return
        reusable = None
        for item in sorted(items, key=lambda x: x.get("lastActiveAt", "")):
            path = Path(item.get("worktree", ""))
            item_ttl = item.get("ttlSeconds", ttl)
            try:
                issued_here = path.resolve().is_relative_to(home)
            except (OSError, RuntimeError):
                issued_here = False
            if issued_here and reclaimable(item, path, now, item_ttl):
                reusable = item
                break
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path(reusable["worktree"]) if reusable else home / f"{thread_slug}-{stamp}"
        if reusable:
            git(repo, "worktree", "remove", "--force", str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        branch = f"thread/{thread_slug}/{stamp}"
        git(repo, "worktree", "add", "-b", branch, str(target), base_ref(repo))
        item = {
            "id": f"{thread_slug}-{stamp}", "repo": str(common), "worktree": str(target),
            "branch": branch, "thread": thread, "status": "active",
            "claimedAt": now_iso(), "lastActiveAt": now_iso(), "ttlSeconds": ttl,
        }
        if reusable:
            Path(reusable["_file"]).unlink(missing_ok=True)
        save(common, item)
        items.append(item)
        warn_lost(items)
    print(target)


def touch(root, common):
    with mutex(common):
        item = current_record(root, common)
        if not item:
            raise SystemExit("current worktree has no lease; run worktree-lease claim first")
        item["lastActiveAt"] = now_iso()
        item["status"] = "active"
        save(common, item)
    return item


def cmd_touch(args):
    root, common = repo_info(args.repo)
    item = touch(root, common)
    print(item["lastActiveAt"])


def delivery_target(root, remote, explicit):
    if explicit:
        return explicit
    result = git(root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False)
    if result.returncode == 0 and result.stdout.strip().startswith(f"{remote}/"):
        return result.stdout.strip().split("/", 1)[1]
    raise SystemExit("cannot determine delivery target; pass --target main or --target dev")


def verify_remote_delivery(root, remote, target):
    ref = f"refs/remotes/{remote}/{target}"
    refspec = f"+refs/heads/{target}:{ref}"
    fetched = git(root, "fetch", "--prune", remote, refspec, check=False)
    if fetched.returncode != 0:
        raise SystemExit((fetched.stderr or fetched.stdout or "delivery fetch failed").strip())
    integrated = git(root, "merge-base", "--is-ancestor", "HEAD", ref, check=False)
    if integrated.returncode != 0:
        raise SystemExit(
            f"worktree HEAD is not integrated into {remote}/{target}; merge or push it to the delivery branch first"
        )
    return git(root, "rev-parse", "HEAD").stdout.strip()


def cmd_release(args):
    root, common = repo_info(args.repo)
    item = current_record(root, common)
    if not item:
        raise SystemExit("current worktree has no lease")
    clean = is_clean(root)
    if not clean:
        if not args.force:
            raise SystemExit("refusing to release a dirty worktree (use --force only to mark it lost)")
        status = "lost"
        delivered_commit = target = None
    elif ahead_commits(root) == 0:
        # Clean and not ahead of the delivery target: nothing to deliver, so
        # demanding --delivery-verified would strand it as a zombie forever.
        status = "released"
        delivered_commit = target = None
    else:
        if not args.delivery_verified:
            raise SystemExit(
                "release requires --delivery-verified after tests, remote integration, CI, and required deployment checks"
            )
        target = delivery_target(root, args.remote, args.target)
        delivered_commit = verify_remote_delivery(root, args.remote, target)
        status = "released"
    with mutex(common):
        item = current_record(root, common)
        if not item:
            raise SystemExit("current worktree lease disappeared during release")
        item["lastActiveAt"] = now_iso()
        item["status"] = status
        if status == "released" and delivered_commit is None:
            item["settledReason"] = "clean and not ahead of delivery target; nothing to deliver"
            item["settledAt"] = now_iso()
        if status == "released" and delivered_commit is not None:
            item["deliveredCommit"] = delivered_commit
            item["deliveryTarget"] = f"{args.remote}/{target}"
            item["deliveryVerifiedAt"] = now_iso()
        save(common, item)
    print(item["status"])


def cmd_status(args):
    root, common = repo_info(args.repo)
    now = time.time()
    data = []
    for item in records(common):
        item.pop("_file", None)
        age = max(0, int(now - parse_time(item["lastActiveAt"])))
        item["ageSeconds"] = age
        path = Path(item.get("worktree", ""))
        ttl = item.get("ttlSeconds", DEFAULT_TTL)
        item["lastCommitAt"] = last_commit_at(path) if path.exists() else None
        item["aheadCommits"] = ahead_commits(path) if path.exists() else None
        if reclaimable(item, path, now, ttl):
            item["effectiveStatus"] = "free"
        elif age > ttl and path.exists() and carries_work(path):
            item["effectiveStatus"] = "lost"
        else:
            item["effectiveStatus"] = item.get("status", "unknown")
        data.append(item)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        for item in data:
            print(f"{item['effectiveStatus']:<12} {item.get('thread', 'legacy'):<32} {item['ageSeconds']:>7}s  {item['worktree']}")


def cmd_git(args):
    root, common = repo_info(".")
    touch(root, common)
    p = subprocess.run(["git", "-C", str(root), *args.git_args])
    touch(root, common)
    raise SystemExit(p.returncode)


def parser():
    p = argparse.ArgumentParser(prog="worktree-lease", description="Bind Git worktrees to threads with automatic TTL recovery")
    p.add_argument("--version", action="version", version=f"worktree-lease {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("claim", help="claim or create an available worktree")
    c.add_argument("repo", nargs="?", default=".")
    c.add_argument("--thread", help="stable thread/session key; defaults to OPENCLAW_SESSION_KEY")
    c.add_argument("--ttl", type=int, default=DEFAULT_TTL)
    c.add_argument(
        "--worktrees-dir",
        default=os.environ.get("WORKTREE_LEASE_HOME"),
        help="override worktree parent; defaults to <repo>-worktrees beside the primary repo",
    )
    c.set_defaults(func=cmd_claim)
    for name, func in (("touch", cmd_touch), ("release", cmd_release)):
        q = sub.add_parser(name)
        q.add_argument("repo", nargs="?", default=".")
        if name == "release":
            q.add_argument("--force", action="store_true", help="mark a dirty worktree lost; never counts as delivery")
            q.add_argument("--target", help="canonical delivery branch, usually main or dev")
            q.add_argument("--remote", default="origin")
            q.add_argument(
                "--delivery-verified",
                action="store_true",
                help="attest that tests, CI, and any required deployment verification passed",
            )
        q.set_defaults(func=func)
    s = sub.add_parser("status")
    s.add_argument("repo", nargs="?", default=".")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)
    g = sub.add_parser("git", help="run git in the current worktree and refresh its lease before and after")
    g.add_argument("git_args", nargs=argparse.REMAINDER)
    g.set_defaults(func=cmd_git)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if getattr(a, "git_args", None) and a.git_args[0] == "--":
        a.git_args = a.git_args[1:]
    a.func(a)


if __name__ == "__main__":
    main()
