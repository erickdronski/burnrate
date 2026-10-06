"""burnrate — what your coding agent actually cost, and a cap to stop it.

Reads the session logs your agent already writes to disk — Claude Code's
transcripts and Codex CLI's rollouts — prices them against a dated model table,
and prints a receipt. Nothing is uploaded, no API key is needed, and there are
no dependencies.

The correctness detail everything rests on: both agents write the same usage
more than once. Claude Code writes each streamed message many times, so summing
usage naively overcounts by roughly 3x (see ``burnrate.sessions``); Codex logs
most responses twice and leaves some out of its running total (see
``burnrate.codex``).

    python3 -m burnrate                    # receipt for the last session
    python3 -m burnrate --today --summary day
    python3 -m burnrate guard --cap 5.00   # hook: stop at a spend cap
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
