# Changelog

## [0.1.0] — 2026-08-14

Initial release.

### Features

- Per-session receipts: cost by model, token breakdown by billing category,
  tools called, files touched, commands run
- `--summary day|project|model` rollups
- `guard --cap` spend cap as a Claude Code hook, with a warning threshold
- `--format json` for downstream tooling
- `--prices` override file for self-hosted or unlisted models

### Correctness

- Deduplicates streamed assistant records by `message.id`, preventing a ~2–3×
  overcount (measured at 2.30× on a 3,721-turn session)
- Prices 5-minute and 1-hour cache writes at their separate multipliers
  (1.25× and 2× of base input)
- Reports unpriced models rather than costing them at zero
- Excludes `<synthetic>` records
- Guard fails open on every condition it cannot evaluate

### Tooling

- 78 tests, standard library only
- CI across Python 3.9–3.13, plus an install-and-run job
