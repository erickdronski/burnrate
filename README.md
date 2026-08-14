<h1 align="center">burnrate</h1>

<p align="center"><strong>What your coding agent actually cost — and a cap to stop it before it costs more.</strong><br>
Local, offline, zero dependencies, no API key.</p>

<p align="center">
  <a href="#try-it">Try it</a> ·
  <a href="#the-spend-cap">Spend cap</a> ·
  <a href="#the-bug-in-every-naive-token-counter">Why other counters are wrong</a> ·
  <a href="#what-it-reports">Reports</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-101828">
  <img alt="zero dependencies" src="https://img.shields.io/badge/dependencies-0-08775c">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-174ea6">
  <img alt="78 tests" src="https://img.shields.io/badge/tests-78-6b21a8">
</p>

---

You start an agent on a task, step away, and come back to a finished feature and
no idea what it cost. Then at some point you get a bill, and it is one number
for a month of work you can no longer break down.

`burnrate` reads the session transcripts your agent already writes to disk and
prints a receipt. Nothing is uploaded, no API key is involved, and it works
offline — the data is already on your machine.

## Try it

```bash
pip install burnrate
burnrate
```

```
────────────────────────────────────────────────────────────────
  nalee   2026-08-14
  f26ccfad · main
────────────────────────────────────────────────────────────────

                                     TOKENS         COST
  claude-opus-5                       47.5M       $37.31

  input (uncached)                      306
  input (cache read)                  46.5M
  cache write (1h)                   722.5k
  output                             273.0k

────────────────────────────────────────────────────────────────
  TOTAL                                           $37.31
────────────────────────────────────────────────────────────────

  Prompt caching saved $205.69 ($242.99 without it, 98% of input served
  from cache).

  153 turns · 158 tool calls · 2 errors

  Tools
    Bash                             64
    Write                            59
    Edit                             25
```

That caching line is usually the surprise. A long agentic session re-reads its
whole context every turn, and cache reads cost a tenth of base input — so the
token count looks alarming and the bill mostly isn't.

## The spend cap

A receipt tells you what a session cost *after* it cost it. The failure people
actually want prevented is the loop that runs for two hours while nobody is
watching.

```bash
burnrate guard --cap 5.00
```

Wire it into `~/.claude/settings.json` as a hook and it checks on every tool
call, warns at 75% of the cap, and stops the session at 100%:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "burnrate guard --cap 5.00" }] }
    ]
  }
}
```

```
burnrate: this session has reached $5.02 against a $5.00 cap.
Stopping here. Raise the cap with --cap, or start a fresh session — context
resets are usually cheaper than continuing a long one anyway.
```

**It fails open, always.** If the transcript is missing, unreadable, or uses a
model with no price on file, the guard exits 0 and says why. A cost tool that
bricks your agent because it couldn't parse a log file deserves to be
uninstalled, and would be. Every one of those paths is a test.

## The bug in every naive token counter

Streaming writes the same assistant message to the transcript repeatedly. On
this machine, **6,244 of 8,822 assistant records shared a `message.id` with
another record.** Summing usage across them — the obvious implementation —
overcounts by roughly 3×.

Measured against a real 3,721-turn session:

```
raw cache_read sum:   4,656,736,734
after deduplication:  2,022,217,272
overcount avoided:    2.30×
```

`burnrate` keeps one usage record per `message.id`, taking the one with the
highest `output_tokens` (streaming writes a growing count, so the largest is the
complete one). If a tool tells you your sessions cost 2–3× what your invoice
says, this is why.

Three more places the arithmetic is easy to get wrong, and what this does:

- **Cache writes have two prices.** A 5-minute cache write costs 1.25× base
  input; a 1-hour write costs 2×. The logs record them separately; tools that
  apply a single multiplier are wrong for whichever TTL they didn't pick, and on
  long sessions the 1-hour writes dominate.
- **Unknown models are never costed at zero.** They're named in the output and
  excluded from the total, so a gap looks like a gap instead of a discount. Add
  one with `--prices`.
- **`<synthetic>` records aren't billable** and are dropped.

## What it reports

```bash
burnrate                     # the most recent session
burnrate --last 5            # the last five
burnrate --today             # everything from today
burnrate --project nalee     # one project
burnrate --summary day       # roll up by day, project, or model
burnrate --verbose           # files touched and commands run
burnrate --format json       # everything, for your own tooling
```

```
────────────────────────────────────────────────────────────────
  BURN BY DAY
────────────────────────────────────────────────────────────────

                          SESSIONS     TOKENS       COST
  2026-08-12                   194     381.1M    $388.04
  2026-08-13                   100     249.3M    $228.04
  2026-08-14                   100     174.2M    $179.49

────────────────────────────────────────────────────────────────
  TOTAL                                         $795.57
────────────────────────────────────────────────────────────────
```

`--verbose` adds what you were actually paying for — the files touched and the
commands run — which is often more interesting than the money.

## Prices

The table is dated, and the date prints on every report, because a cost figure
that doesn't say when its prices were current is a number with a hidden expiry.
Override or extend it:

```json
{ "prices": { "my-self-hosted-model": { "input": 0.5, "output": 1.5 } } }
```

```bash
burnrate --prices prices.json
```

Rates are US dollars per million tokens. Everything this prints is an estimate
from your local logs, not an invoice — discounts, contracts, and platform
differences aren't visible from here.

## Privacy

It reads `~/.claude/projects/**/*.jsonl` and writes nothing. There is no network
code in this package at all — no telemetry, no update check, no analytics. The
only way data leaves your machine is if you pipe the JSON somewhere yourself.

## Testing

```bash
python -m unittest discover -s tests -t .   # 78 tests
```

Pricing tests check against hand-computed rates rather than snapshots — a
snapshot would happily lock in a wrong cache multiplier, which is the most
likely error in the whole project. Parser tests build real transcripts on disk,
including truncated final lines, because live logs are appended to while you
read them.

## License

MIT.
