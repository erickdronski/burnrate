<h1 align="center">burnrate</h1>

<p align="center"><strong>What your coding agent actually cost — and a cap to stop it before it costs more.</strong><br>
Local, offline, zero dependencies, no API key.</p>

<p align="center">
  <a href="#try-it">Try it</a> ·
  <a href="#the-spend-cap">Spend cap</a> ·
  <a href="#the-bug-in-every-naive-token-counter">Why other counters are wrong</a> ·
  <a href="#codex">Codex</a> ·
  <a href="#what-it-reports">Reports</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img alt="MIT license" src="https://img.shields.io/badge/license-MIT-101828">
  <img alt="zero dependencies" src="https://img.shields.io/badge/dependencies-0-08775c">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-174ea6">
  <img alt="Linux macOS Windows" src="https://img.shields.io/badge/tested_on-Linux%20%7C%20macOS%20%7C%20Windows-0f766e">
  <img alt="ruff" src="https://img.shields.io/badge/lint-ruff-d97706">
  <img alt="204 tests" src="https://img.shields.io/badge/tests-204-6b21a8">
</p>

---

You start an agent on a task, step away, and come back to a finished feature and
no idea what it cost. Then at some point you get a bill, and it is one number
for a month of work you can no longer break down.

`burnrate` reads the session logs your agent already writes to disk — Claude
Code's transcripts and OpenAI Codex CLI's rollouts — and prints a receipt.
Nothing is uploaded, no API key is involved, and it works offline — the data is
already on your machine.

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
  f26ccfad · main · Claude Code
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
whole context every turn, and cache reads cost a tenth of base input or less —
so the token count looks alarming and the bill mostly isn't.

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

The cap is a Claude Code hook, and it reads Claude Code transcripts only.
Codex sessions get receipts but no cap — there is no hook to install it in, and
no verified price to cap against.

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

More places the arithmetic is easy to get wrong, and what this does:

- **Cache reads are priced per model.** Fable 5.1 reads the cache at 0.025×
  base input, Opus 5.5 at 0.05×, everything else at 0.1×. On this machine 96%
  of all input tokens are cache reads, so this one rate decides the bill:
  reading Fable 5.1's cache at the usual tenth overstated its spend by 47%.
- **Cache writes have two prices.** A 5-minute cache write costs 1.25× base
  input; a 1-hour write costs 2×. The logs record them separately; tools that
  apply a single multiplier are wrong for whichever TTL they didn't pick, and on
  long sessions the 1-hour writes dominate.
- **A new point release is not its predecessor.** `claude-opus-5-5` starts with
  `claude-opus-5`, and a prefix lookup prices it at the older model's rates —
  which overstated Opus 5.5 by 80% here. Model names are matched exactly, after
  stripping a date (`-20250929`) or deployment suffix (`[1m]`, `@20251101`).
  A release the table doesn't know is reported as unpriced, not guessed at.
- **Fast mode is priced per turn.** It is a per-request setting, so one session
  can mix fast and standard turns on the same model. Pricing the whole model
  fast because four turns were billed a 2,129-turn session $804 too high.
- **Unknown models are never costed at zero.** They're named in the output and
  excluded from the total, so a gap looks like a gap instead of a discount. Add
  one with `--prices`.
- **`<synthetic>` records aren't billable** and are dropped.

## Codex

`burnrate` reads OpenAI Codex CLI rollouts too: `~/.codex/sessions`, and
`~/.codex/archived_sessions`, because archiving a session in the app does not
un-spend it. With no flags it reads every agent it finds; `--agent codex` or
`--agent claude` narrows to one.

A Codex *session* is a conversation and every subagent thread it spawned. Each
thread is written to its own file and tagged with the conversation's
`session_id`, and the receipt reports the session, because that is what
somebody started. On this machine 376 rollout files held 47 sessions.

```
────────────────────────────────────────────────────────────────
  my-app   2026-09-30 → 2026-10-06
  0f98003f · Codex · 78 threads
────────────────────────────────────────────────────────────────

                                     TOKENS         COST
  gpt-6-astra                        689.9M     unpriced

  input (uncached)                    19.5M
  input (cache read)                 668.4M
  output                               2.0M

────────────────────────────────────────────────────────────────
  TOTAL                                         unpriced
────────────────────────────────────────────────────────────────

  ! Not included in the total — no price on file for: gpt-6-astra
    Supply one with --prices to include it.

  Codex plan usage (pro): 62% of the weekly limit, as of
  2026-10-06 21:34 UTC; resets 2026-10-10 04:26 UTC.

  5129 turns · 4818 tool calls
```

**Codex logs most responses twice, and its running total leaves some out.**
Every `token_count` event carries `total_token_usage`, cumulative for the
thread, and `last_token_usage` for the latest response; CLI 0.15x also writes a
`token_usage_record` per response, keyed by `response_id`. Measured across all
of this machine's rollouts:

```
sum of every token_count's last_token_usage     9,498,661,274
  plus every token_usage_record                18,289,278,830
final total_token_usage of each file            8,554,588,845
burnrate: one usage per response_id             8,790,617,556
```

- **Counting both sources doubles the bill (2.08×).** Every `token_count` that
  advanced the total — 60,351 of them — came immediately after the record for
  the same response, with identical numbers.
- **Summing `last_token_usage` overcounts by 8%.** 5,827 events re-emit the
  previous total with no response behind them, and after a compaction the event
  reports the size of the compacted context as `last`, with no model call.
- **The final total undercounts by 236M tokens (2.7%).** The 638 compaction
  calls are in the records but never reach the running total, and a session
  resumed after a restart starts it again from zero.
- **Summing `total_token_usage` per event** — reading a cumulative counter as a
  per-response one — would overstate usage 1,735×.

`burnrate` counts one usage per `response_id`. A `token_count` is used only when
no record arrived since the previous one — every event, on a CLI old enough not
to write records — and only when its total actually moved.

Forked subagent threads open with a copy of the parent's history. On CLI
0.153–0.160 that copy is messages only: all 60,990 records carry their own
file's thread id, and no response id appears in two files. That is enforced
rather than trusted — a record with another thread's id is dropped, response
ids are deduplicated across a session's files, and without records a fork's
events from before `subagent_history_start_ordinal` are not counted.

**What the counters mean**, checked rather than assumed:
`reasoning_output_tokens` never exceeds `output_tokens` (66,178 of 66,178
events), and `total_tokens` is exactly `input_tokens + output_tokens` on every
event that stands for a model call — so reasoning is part of output and is not
added again. `cached_input_tokens` never exceeds `input_tokens`: cached input is
part of input, and uncached input is the difference.
`cache_write_input_tokens` is zero in every record; if it ever is not, it stays
in uncached input, which understates rather than inflates.

**No Codex model is priced.** No OpenAI rate in this package has been verified
by anyone, so Codex models are listed with their tokens and left out of every
total — the same treatment as any unknown model, for the same reason. Supply
your rates and they are priced like anything else:

```json
{ "prices": { "gpt-6-astra": { "input": …, "cached_input": …, "output": … } } }
```

Each `…` is your rate in US dollars per million tokens. Copied as is, the file
is rejected rather than read as zeros, which would cost billions of tokens at
nothing.

**Plan usage is the number a subscriber can act on.** Codex logs the plan's own
meter with every response, and burnrate prints the latest reading. It is the
reading from when the log was written, not a live query — this package makes no
network calls — so it says when it was taken and whether the window has rolled
over since. It prints on each Codex receipt and under every roll-up.

Tool calls are counted from `function_call` and `custom_tool_call` items.
Recent CLIs run most tools from generated JavaScript inside one `exec` call, so
`exec` leads the tool list; `--verbose` pulls the shell commands out of that
code (`tools.exec_command({cmd: …})`) and the file names out of patches, and
every command is redacted at capture, as for Claude Code.

Rollouts run to gigabytes — one session here is 5.6 GB, nearly all of it tool
output and encrypted reasoning. Each line's type is read from an anchored match
on its first few dozen bytes, and only the four kinds of record a receipt uses
are decoded. A line that doesn't match that exact compact prefix is decoded in
full, so a format change costs speed, not correctness — the same rule as the
Claude fast path below.

## What it reports

```bash
burnrate                     # the most recent session
burnrate --last 5            # the last five
burnrate --today             # everything from today
burnrate --project nalee     # one project
burnrate --top               # the sessions that cost the most
burnrate --trend             # daily spend, and whether it is rising
burnrate --summary day       # roll up by day, project, model, or agent
burnrate --agent codex       # one agent (default: every agent found)
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

Measured on one machine's full history — 4,488 Claude Code transcripts (6.6 GB)
and 376 Codex rollouts (11.8 GB) — with Python 3.12. CPU time is the stable
figure; wall time depends on how much of the history the page cache holds, and
on a heavily loaded machine it ran 1.3–2.7× the CPU time.

| | CPU time |
|---|---|
| `burnrate` (most recent session) | **0.2s** |
| `burnrate --agent codex` (most recent Codex session: 78 threads, 690M tokens) | **1.6s** |
| `burnrate --agent claude --summary day` (entire Claude Code history) | **22s** (was 24s) |
| `burnrate --agent codex --summary day` (entire Codex history) | **16s** |
| `burnrate --summary day` (both) | **38s** |

At 0.1.0 the history was 2,448 transcripts and the whole-history scan took
10.4s, down from 36.6s. Two things dominated that profile, and both are worth
knowing about because they are easy to get wrong:

- **Redaction ran 441,000 regex substitutions**, nearly all on commands like
  `npm test` that contain nothing secret-shaped. A single cheap pre-check now
  skips the full pass, and a test re-runs every positive case with the
  pre-check disabled to prove it cannot mask a real secret by accident. In
  0.2.0 that pre-check became plain substring tests: Codex captures long
  commands, and a 30-branch case-insensitive regex was itself the cost.
  Redaction CPU fell from 12.6s to 7.9s, with identical output on all 117,891
  commands.
- **`json.loads` ran on every line**, including queue operations and titles
  that can never carry usage. A substring test on the raw line is roughly two
  orders of magnitude cheaper, and it is conservative: anything that does not
  clearly announce an uninteresting type still gets parsed, so a format change
  costs speed rather than correctness. Codex lines are classified the same way
  by an anchored match on their first few dozen bytes, which costs the same
  for a 40-byte line as a 40-megabyte one; reading the 11.8 GB at all is now
  most of the Codex time.

`--since` and `--today` skip any file last written more than a day before the
date without opening it.

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

When Codex sessions are in the window, `--top` gains an AGENT column, `--trend`
splits each day's sessions by agent, and every roll-up names the models it
could not price, with their tokens, rather than leaving them out of a total
that looks complete.

## Prices

The table is dated, and the date prints on every report, because a cost figure
that doesn't say when its prices were current is a number with a hidden expiry.
Current as of **2026-09-25**, in US dollars per million tokens:

| Model | Input | Output | Cache read |
|---|---:|---:|---:|
| `claude-fable-5-1`, `claude-mythos-5-1` | $10 | $50 | $0.25 |
| `claude-fable-5`, `claude-mythos-5` | $10 | $50 | $1.00 |
| `claude-opus-5-5` | $4 | $20 | $0.20 |
| `claude-opus-5`, `claude-opus-4-8`/`4-7`/`4-6`/`4-5` | $5 | $25 | $0.50 |
| `claude-sonnet-5-5`, `claude-sonnet-5` | $2 | $10 | $0.20 |
| `claude-sonnet-4-6`, `claude-sonnet-4-5` | $3 | $15 | $0.30 |
| `claude-haiku-4-5` | $1 | $5 | $0.10 |
| fast mode: `claude-opus-5-5` | $8 | $40 | $0.40* |
| fast mode: `claude-opus-5`, `claude-opus-4-8` | $10 | $50 | $1.00 |

Cache writes are 1.25× base input for the 5-minute TTL and 2× for the 1-hour
TTL on every model, fast mode included. \*The published rates give no fast-mode
cache-read price for Opus 5.5; burnrate applies the model's 0.05× read ratio to
the fast input rate and says so here rather than presenting $0.40 as quoted. A
fast turn on a model with no fast price (Opus 4.7's fast mode has been removed)
is reported as unpriced.

Override or extend the table:

```json
{ "prices": {
    "my-self-hosted-model": { "input": 0.5, "output": 1.5 },
    "claude-opus-5-5": { "input": 3.2, "output": 16, "cache_read": 0.16 }
} }
```

```bash
burnrate --prices prices.json
```

`cache_read` is optional (`cached_input` is accepted too) and defaults to a
tenth of `input`, which is what every override meant before cache reads were
priced per model. Rates are US dollars per million tokens. Everything this
prints is an estimate from your local logs, not an invoice — discounts,
contracts, and platform differences aren't visible from here.

## Privacy

It reads `~/.claude/projects/**/*.jsonl`, `~/.codex/sessions`, and
`~/.codex/archived_sessions`, and writes nothing. There is no network
code in this package at all — no telemetry, no update check, no analytics. The
only way data leaves your machine is if you pipe the JSON somewhere yourself.

**Secrets in captured commands are masked.** `--verbose` and `--format json`
print the shell commands your agent ran — Claude Code's `Bash` calls, and the
commands Codex ran through its shell tools — and agent sessions routinely contain
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
python -m unittest discover -s tests -t .   # 204 tests
```

Pricing tests check against hand-computed rates rather than snapshots — a
snapshot would happily lock in a wrong cache multiplier, which is the most
likely error in the whole project. Parser tests build real transcripts on disk,
including truncated final lines, because live logs are appended to while you
read them. The Codex fixtures reproduce the record sequences measured on real
rollouts — the duplicate counts, the re-emitted totals, the compaction call only
the records see, a resumed session's reset — and the shapes older CLIs wrote.

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
