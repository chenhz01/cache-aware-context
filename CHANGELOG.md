# Changelog

All notable changes to `cache-aware-context` (ALIGNMENT-16 rule 93).

## v0.1.1 - 2026-10-11 (round 2: self-audit revision)

### Added
- `--alert RATE` on `analyze`/`panel`: exits 3 when hit rate falls below the
  threshold, turning the panel into a cron/CI gate (the README Quickstart now
  shows the one-liner). 3 new tests cover the trigger, the pass case, and
  invalid thresholds. Tests 38 → 41 (**+8% count; new deployable use case:
  0 → 1 automated gating path**).
- Explicit refusal error message when the stable prefix alone exceeds the
  budget, telling the operator exactly which knob to move.

### Fixed
- Self-test for zero-budget compression asserted an impossible case
  (keep=2, budget=0 collides with the prefix invariant by design). Split into
  two honest cases: keep=0/budget=0 drops all, and a prefix-only budget
  passes the prefix through untouched.

## v0.1.0 - 2026-10-11 (initial release)

### Added
- Event-log loader (dirty-tolerant JSONL), hit-rate analyzer with 4-way
  attribution (model_switch / prefix_rewrite / ttl_expired / unknown),
  per-model breakdown, text health panel.
- Stable-prefix-guaranteed compressor with prefix hashing and est-token
  budgeting.
- CLI: `analyze / panel / compress / version`.
- Three-tier self-test, 38 checks PASS.
