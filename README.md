# worktree-lease

Dependency-free Git worktree lease manager. Binds a Git worktree to a stable
thread/session key so that concurrent agents (or humans) never collide on the
same checkout, and recovers abandoned worktrees **without ever destroying
undelivered work**.

Pure Python 3 standard library. No runtime dependencies.

## Why

Multiple agent sessions working on the same repository need isolated checkouts.
Naive automation recycles "idle" worktrees and deletes commits that were never
pushed. `worktree-lease` treats *clean* and *delivered* as different facts, and
refuses to recycle anything that might still carry work.

## Install

```bash
pipx install git+https://github.com/exisz/worktree-lease
# or
python3 -m pip install --user git+https://github.com/exisz/worktree-lease
```

This installs the `worktree-lease` console script.

## Quick start

```bash
# Claim (or reuse) this thread's worktree
worktree-lease claim /path/to/repo --thread my-session-key

# Run git through the lease so activity keeps refreshing it
worktree-lease git -- status
worktree-lease git -- push origin HEAD

# Inspect all leases for the repo
worktree-lease status --json

# Finish
worktree-lease release --target main --delivery-verified
```

Repeating `claim` with the same `--thread` returns that thread's existing
worktree rather than creating a new one.

## State model

A lease record is JSON stored in `<git-common-dir>/worktree-leases/`.

| Status | Meaning |
| --- | --- |
| `active` | Held by a thread; refreshed by `claim`, `touch`, and `git`. |
| `released` | Finished. Either delivery was verified, or there was nothing to deliver. |
| `lost` | Expired **and** still carrying work. Requires human review; never auto-removed. |

Derived facts, computed live from the worktree:

- **dirty** — `git status --porcelain` is non-empty.
- **ahead** — `ahead_commits()`: commits on `HEAD` not contained in the delivery
  target (`origin/main`, `origin/master`, or `origin/HEAD`). Returns `-1` when
  the answer cannot be established.
- **carries work** — dirty OR `ahead != 0`. Note `-1 != 0`, so an
  *undeterminable* ahead count counts as carrying work. Uncertainty is never
  resolved in favour of deletion.

### Reclaim rule

A worktree may be recycled only when **all** hold:

1. its path exists,
2. it is clean,
3. `ahead_commits() == 0` (definitely delivered — not `-1`),
4. it is `released`, **or** both its lease and its last commit are older than the TTL.

Anything else is left alone. This is the safety property the tool exists for.

### Lost and settling

An expired lease that still carries work is marked `lost` and reported on stderr
(`LOST_WORKTREE:`) so a human can investigate. A `lost` record whose worktree has
since become clean and not-ahead is auto-demoted back to the reusable pool by
`reap_settled()` — otherwise zombies would accumulate forever.

### Primary worktree freshness

Remote-first delivery pushes thread branches straight to the delivery branch, so
nothing naturally advances the repository's *primary* worktree. Left alone it
drifts monotonically behind and every human who opens the repo sees stale code.

`claim` and a verified `release` therefore fetch and **fast-forward the primary
worktree** when it is clean and on the delivery branch. This is best effort and
never fatal.

Divergence is never silently merged. When the primary worktree has both local
and remote commits, or is behind but dirty, the tool prints `DIVERGED_PRIMARY:`
on stderr and leaves it untouched. `status` reports the same warning for a
primary that is behind or diverged.

### Release rule

| Worktree state | Result |
| --- | --- |
| dirty | Refused. `--force` marks it `lost`; that is never delivery. |
| clean, `ahead == 0` | `released` immediately — nothing to deliver. |
| clean, ahead or undeterminable | Requires `--delivery-verified`; the tool then fetches and asserts `HEAD` is an ancestor of the remote delivery branch. |

## Commands

### `claim [repo] [--thread KEY] [--ttl SECONDS] [--worktrees-dir DIR]`

Returns the path to this thread's worktree, creating one if needed. Thread key
defaults to `$OPENCLAW_SESSION_KEY` / `$OPENCLAW_THREAD_ID`. New worktrees go to
`<repo>-worktrees/` beside the primary repository unless overridden by
`--worktrees-dir` or `$WORKTREE_LEASE_HOME`.

Before resolving the base commit it fetches `origin` and fast-forwards the
primary worktree, so a new worktree always branches from the freshest known
remote tip.

### `touch [repo]`

Refreshes the current worktree's lease timestamp.

### `release [repo] [--target BRANCH] [--remote NAME] [--delivery-verified] [--force]`

Closes out the lease per the release rule above.

### `status [repo] [--json]`

Lists leases with `effectiveStatus`, `ageSeconds`, `lastCommitAt`, and
`aheadCommits`. Warns on stderr with `DIVERGED_PRIMARY:` when the primary
worktree is behind or diverged from its upstream.

### `git -- <git args>`

Runs git in the current worktree, refreshing the lease before and after.

## Guard rails

- Refuses to lease an OpenClaw **agent workspace** repository; leases are for
  project/source repositories only.
- Lease mutations are serialised by a directory mutex with a stale-lock timeout.
- Records are written atomically via temp-file + `os.replace`.

## Environment

| Variable | Purpose |
| --- | --- |
| `WORKTREE_LEASE_TTL` | Default lease TTL in seconds (default `86400`). |
| `WORKTREE_LEASE_HOME` | Default parent directory for new worktrees. |
| `OPENCLAW_SESSION_KEY` / `OPENCLAW_THREAD_ID` | Default thread key. |
| `OPENCLAW_CONFIG_PATH` / `OPENCLAW_STATE_DIR` | Locate OpenClaw config for the workspace guard. |

## Development

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest tests/ -v
```

Tests create real temporary Git repositories; there are no mocks.

## License

MIT
