# Changelog

## [0.2.0] — 2026-10-06

Codex support, and a price table that was wrong where it mattered most.

### Features

- Reads OpenAI Codex CLI rollouts from `~/.codex/sessions` and
  `~/.codex/archived_sessions`; a Codex session is a conversation plus every
  subagent thread it spawned
- `--agent claude|codex|all` (default: every agent whose logs are present) and
  `--codex-root`; receipts, `--top`, `--trend` and `--summary` label the agent,
  and `--summary agent` rolls up by it
- Codex plan usage from the latest rate-limit reading in the logs: how much of
  the weekly (or 5-hour) window is used, as of when, and when it resets
- Codex tool calls counted by name; `--verbose` pulls shell commands out of
  code-mode `exec` calls and file names out of patches
- Receipt headers show the days a session ran (`2026-09-05 → 2026-10-06`)
  rather than only the day it began
- `--prices` entries take an optional `cache_read` (or `cached_input`)

### Correctness

- Cache reads priced per model — 0.025× input on Fable 5.1 and Mythos 5.1,
  0.05× on Opus 5.5, 0.1× elsewhere — instead of a global 0.1×
- Model lookup is exact after stripping a date or deployment suffix. Prefix
  matching priced Opus 5.5 as Opus 5 (overstated 80%) and Fable 5.1 as Fable 5;
  an unknown point release is now reported as unpriced
- Fast mode priced per turn: one fast turn no longer reprices every turn of the
  model in that session (one session was overstated by $804)
- Prices dated 2026-09-25: adds Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1,
  and Opus 5.5 fast mode; corrects Sonnet 5 to $2/$10 (was $3/$15). A fast turn
  on a model with no fast-mode price is unpriced rather than billed standard
- Net effect on the measuring machine's Claude Code history, on the same parse:
  $20,257.56 before, $13,498.73 after
- Codex usage counted once per `response_id`: counting `token_count` and
  `token_usage_record` together overcounts 2.08×, summing `last_token_usage`
  overcounts 8%, and reading final totals undercounts 2.7% (measured across
  376 rollouts)
- Codex models are reported by name and token count as unpriced — no OpenAI
  rate here has been verified
- `--top`, `--trend` and `--summary` name unpriced models and their tokens
  instead of leaving them out of the totals without a word
- Subagent transcripts are attributed to their project, not to `subagents` or a
  fragment of a workflow id — 4,411 of 4,488 transcripts on the measuring
  machine, which `--project` had been missing

### Performance

- Codex lines are classified by an anchored match on their compact prefix and
  only four record kinds are decoded; a line that does not match is decoded in
  full
- Secret screening uses substring tests for ASCII text: identical output on
  117,891 real commands, redaction CPU 12.6s → 7.9s
- `--since` and `--today` skip files untouched since the date without reading
  them

### Tooling

- 204 tests (was 129), including on-disk Codex fixtures for current and older
  CLI formats and truncated final lines
- A test that `--version`, the package and `pyproject.toml` agree

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
