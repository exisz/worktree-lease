# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning is [SemVer](https://semver.org/).

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
