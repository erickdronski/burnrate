"""burnrate — what your coding agent actually cost, and a cap to stop it.

Reads the session transcripts your agent already writes to disk, prices them
against a dated model table, and prints a receipt. Nothing is uploaded, no API
key is needed, and there are no dependencies.

The correctness detail everything rests on: streaming writes the same assistant
message to the transcript many times, so summing usage naively overcounts by
roughly 3x. See ``burnrate.sessions`` for the deduplication rule.

    python3 -m burnrate                    # receipt for the last session
    python3 -m burnrate --today --summary day
    python3 -m burnrate guard --cap 5.00   # hook: stop at a spend cap
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
