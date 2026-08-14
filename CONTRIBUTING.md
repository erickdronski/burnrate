# Contributing

The most valuable contributions are **support for other agent harnesses** and
**price table corrections**.

## The bar

**Never break fail-open.** The guard must exit 0 on any condition it cannot
evaluate — missing transcript, unparseable log, unpriced model, unexpected
exception. A cost tool that blocks someone's agent because of its own bug is
worse than no cost tool. Every fail-open path has a test; keep it that way.

**Pricing changes need hand-computed tests.** Not snapshots. A snapshot test
locks in whatever the code did the day it was written, including a wrong cache
multiplier.

**Parser changes need a real transcript fixture.** `tests/test_sessions.py`
builds actual files on disk, including malformed and truncated ones. Testing the
parser against a mock tests the mock.

**No dependencies.** Standard library only; CI fails if a `requirements.txt`
appears. **Python 3.9 compatible** — no `X | Y` unions, no `match`.

**No network code.** This package makes no outbound connections, and that claim
is in the README. A PR that adds one — telemetry, an update check, a price
fetch — will be declined regardless of how useful it is.

## Adding another harness

`burnrate/sessions.py` parses Claude Code's JSONL format. To support another
agent, add a parser that produces the same `Session` / `Turn` / `Usage` objects
and register its default transcript location in `DEFAULT_ROOTS`. The pricing,
receipt, and guard layers are format-agnostic.

Before you start, check that the format actually records per-response token
usage. Without it, any cost figure is an estimate of an estimate, and this tool
would rather report nothing than a plausible fiction.

## Updating prices

Update `PRICES` **and** `PRICES_AS_OF` together — the date prints on every
report and a stale date is worse than a stale price, because it hides the
staleness. Include a link to the published rates in the PR.

## Style

Explain *why* in comments, not *what*. The dedup rule in `sessions.py` is the
model: the code is four lines, the comment explains the 3× error it prevents.
