# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning is [SemVer](https://semver.org/).

## [1.1.0] - 2026-08-20

### Fixed

- **Stale primary worktree.** Remote-first delivery pushes thread branches
  directly to the delivery branch, so nothing ever advanced the repository's
  primary worktree. It drifted monotonically behind — observed 60+ commits
  behind `origin/main` on a real repository. `claim` and a verified `release`
  now fast-forward the primary worktree when it is clean and on the delivery
  branch.
- `claim` resolved the base commit from cached remote refs without fetching, so
  a new worktree could silently branch from a stale baseline. It now fetches
  `origin --prune` first, making remote-first branching a guarantee rather than
  an accident.

### Added

- `sync_primary()` — best-effort, fast-forward-only, clean-only advance of the
  primary worktree. Never fatal.
- `primary_drift()` — `status` now warns `DIVERGED_PRIMARY:` when the primary
  worktree is behind or diverged from its upstream.
- Divergence is reported, never silently merged: a primary that is both ahead
  and behind, or behind but dirty, is left untouched with a `DIVERGED_PRIMARY:`
  warning alongside the existing `LOST_WORKTREE:` reporting.
- Five regression tests covering fast-forward on claim, fresh-tip branching,
  diverged/behind warnings, and refusal to merge a diverged or dirty primary.

## [1.0.0] - 2026-08-20

First packaged release. Previously an unversioned, untested script in `~/.local/bin`.

### Fixed

- **Data loss.** `reclaimable()` considered only working-tree dirtiness and never
  undelivered commits, so a clean-but-expired worktree holding unpushed commits
  could be destroyed by `git worktree remove --force`. Clean is not delivered.
- `release` deadlocked clean, nothing-to-deliver worktrees into permanent
  zombies because it always demanded `--delivery-verified`.

### Added

- `ahead_commits(path)` — commits not contained in the delivery target; returns
  `-1` when undeterminable, which callers must treat as "carries work" and never
  auto-reclaim.
- `carries_work(path)` — dirty OR ahead != 0.
- `reap_settled()` — records marked `lost` that are now clean AND not ahead are
  demoted back to the reusable pool instead of accumulating as zombies.
- `status --json` now reports `aheadCommits`.
- `--version` flag.

### Changed

- `lost` now means dirty **or** holding undelivered commits (previously dirty only).
- Packaged as a real Python project with a test suite and CI.
