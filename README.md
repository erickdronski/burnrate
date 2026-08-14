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
  <img alt="Linux macOS Windows" src="https://img.shields.io/badge/tested_on-Linux%20%7C%20macOS%20%7C%20Windows-0f766e">
  <img alt="ruff" src="https://img.shields.io/badge/lint-ruff-d97706">
  <img alt="78 tests" src="https://img.shields.io/badge/tests-129-6b21a8">
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
pip install git+https://github.com/erickdronski/burnrate
burnrate
```

Installing from git is the supported path today — this is not on PyPI yet, and
the obvious name there belongs to an unrelated project, so `pip install burnrate`
would get you someone else's package. When it is published the distribution
name will be `agent-burnrate`.

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
burnrate --top               # the sessions that cost the most
burnrate --trend             # daily spend, and whether it is rising
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

## Performance

Measured on 2,448 real transcripts (412 sessions, ~1.1M lines):

| | time |
|---|---|
| `burnrate` (most recent session) | **0.17s** |
| `burnrate --summary day` (entire history) | **10.4s** |

The whole-history scan started at 36.6s. Two things dominated the profile, and
both are worth knowing about because they are easy to get wrong:

- **Redaction ran 441,000 regex substitutions**, nearly all on commands like
  `npm test` that contain nothing secret-shaped. A single cheap pre-check now
  skips the full pass, and a test re-runs every positive case with the
  pre-check disabled to prove it cannot mask a real secret by accident.
- **`json.loads` ran on every line**, including queue operations and titles
  that can never carry usage. A substring test on the raw line is roughly two
  orders of magnitude cheaper, and it is conservative: anything that does not
  clearly announce an uninteresting type still gets parsed, so a format change
  costs speed rather than correctness.

Output was verified byte-identical across all 412 sessions before and after.

## What actually cost you money

A day-by-day total tells you *that* Tuesday was expensive. These answer the
questions you can act on.

```bash
burnrate --top      # which sessions cost the most
burnrate --trend    # is my spend rising?
```

```
  DATE       PROJECT           TURNS    TOKENS     COST
  2026-06-28 nalee              3873   2158.5M $2,043.47  36%
  2026-07-02 lore               3191   1724.5M $1,440.77  25%
  2026-06-28 scout              1124    587.9M   $484.73   9%

  These 3 session(s) are 70% of $5,699.18 across 412 session(s).
```

```
  2026-08-12   $388.04 ███░░░░░░░░░░░░░░░░░   194 session(s)
  2026-08-13 $2,556.38 ███████████████████░   103 session(s)
  2026-08-14 $2,756.25 ████████████████████   115 session(s)

  Daily average is up 610% across this window ($388.04 -> $2,756.25).
  5 session(s) spanned more than one day and are counted on the day they
  last ran.
```

That last line matters. A long session gets resumed across weeks, and
attributing its whole cost to the day it *began* dumps months of spend onto one
old bar. Sessions are counted on the day they last ran, and the number that
span more than one day is reported rather than hidden — because a multi-day
session's cost genuinely did not happen on a single day, and no bucketing
choice makes that untrue.

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

**Secrets in captured commands are masked.** `--verbose` and `--format json`
print the shell commands your agent ran, and agent sessions routinely contain
`export ANTHROPIC_API_KEY=sk-...` or a `curl` with a bearer token. Since a cost
report is exactly the kind of file that gets pasted into an issue, every command
is redacted **at capture** — the raw value never enters the object graph, so no
output path can leak it, including one added later.

```
export ANTHROPIC_API_KEY=sk-…redacted…
curl -H "Authorization: Bearer eyJ…redacted…" https://api.example.com
```

Provider-prefixed keys, JWTs, AWS key ids, URL-embedded passwords, and
`SECRET=`-style assignments are covered. Ordinary commands are left alone —
`git checkout <sha>`, `pytest -k password`, and `export API_KEY=${API_KEY}` all
survive intact, because a redactor that mangles normal output gets switched off
and then protects nothing.

**It is best-effort, not a guarantee.** A secret with no recognizable shape,
assigned to an innocuously named variable, will pass through. Deliberately: the
alternative is redacting anything long and random, which would eat commit SHAs
and UUIDs. Treat a report as sensitive before sharing it.

## Testing

```bash
python -m unittest discover -s tests -t .   # 129 tests
```

Pricing tests check against hand-computed rates rather than snapshots — a
snapshot would happily lock in a wrong cache multiplier, which is the most
likely error in the whole project. Parser tests build real transcripts on disk,
including truncated final lines, because live logs are appended to while you
read them.

## Related

Part of a set of small, standalone tools for working with coding agents:

| Tool | Job |
|---|---|
| [agentsmith](https://github.com/erickdronski/agentsmith) | Derives your AGENTS.md from the repo and detects drift |
| [contexttest](https://github.com/erickdronski/contexttest) | A/B tests whether an AGENTS.md change actually helps |
| [tripwire](https://github.com/erickdronski/tripwire) | Audits what your agent is allowed to do |
| [gtm-skills](https://github.com/erickdronski/gtm-skills) | Go-to-market skills for agents, on a tested arithmetic engine |

## License

MIT.
