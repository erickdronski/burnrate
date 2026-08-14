"""Masking secrets before they are printed.

This tool captures the shell commands an agent ran and prints them in
``--verbose`` and ``--format json``. That is genuinely useful — the commands
are what you were paying for — and it is also the most dangerous thing in the
package, because agent sessions routinely contain lines like::

    export ANTHROPIC_API_KEY=sk-ant-...
    curl -H "Authorization: Bearer eyJhbGci..." https://api.example.com

A cost report gets pasted into issues, shared with teammates, and committed to
repos. Printing raw shell history into it would turn a convenience into a
credential leak, and "my machine happened to have no secrets in it" is not a
safety property.

So every captured command passes through here first. The patterns target
credential *shapes* that are unambiguous — provider-prefixed keys, JWTs, AWS
access key ids, `KEY=value` assignments for secret-sounding names — rather than
anything long and random, because over-redaction would destroy the feature.

This is deliberately not a general-purpose secret scanner. It is a last line of
defense on one output path, and it is documented as such: the redaction is
best-effort, and the README says so rather than promising more than it can do.
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

__all__ = ["redact", "redact_all", "PATTERNS"]


def _mask(value: str, keep: int = 3) -> str:
    """Keep a short prefix so a reader can still tell keys apart."""
    value = value.strip()
    if len(value) <= keep + 2:
        return "*" * len(value)
    return value[:keep] + "…redacted…"


#: (pattern, group index to mask). Group 0 means mask the whole match.
PATTERNS: Tuple[Tuple[Pattern, int], ...] = (
    # Provider-prefixed API keys. Unambiguous by construction.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), 0),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), 0),
    (re.compile(r"\bASIA[0-9A-Z]{12,}"), 0),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), 0),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"), 0),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), 0),
    # JSON Web Tokens — three base64url segments.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    # Credentials embedded in a URL: postgres://user:pass@host, https://u:p@h
    (
        re.compile(
            r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@'\"]+:)([^\s@'\"]{3,})(@)"
        ),
        2,
    ),
    # Private key blocks.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    # Authorization headers, in any of the shapes a shell line takes.
    (re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)([^\s\"']{8,})"), 2),
    # Assignments to secret-sounding names: KEY=..., --token=..., "password": "..."
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|"
            r"CREDENTIAL|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|AUTH)[A-Z0-9_]*)"
            r"(\s*[=:]\s*[\"']?)"
            r"([^\s\"';|&]{6,})"
        ),
        3,
    ),
    # Long-form flags: --token abc123, --password hunter2
    (
        re.compile(
            r"(?i)(--(?:token|password|api-?key|secret|auth)[= ])([^\s\"';|&]{6,})"
        ),
        2,
    ),
)

#: Values that match an assignment pattern but carry no secret. Redacting these
#: would make the output worse for no gain.
_SAFE_VALUES = frozenset(
    {
        "true", "false", "null", "none", "nil", "yes", "no", "0", "1",
        "changeme", "example", "placeholder", "test", "dummy", "redacted",
        # Auth *scheme* words. `Authorization: Bearer $TOKEN` otherwise trips
        # the assignment rule — "Authorization" contains AUTH — and masks the
        # scheme instead of the value, which hides nothing and looks broken.
        "bearer", "basic", "digest", "token",
    }
)

_REFERENCE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$|^\$\(")


def redact(text: str) -> str:
    """Return ``text`` with credential-shaped substrings masked.

    Best-effort by design. It will not catch a secret with no recognizable
    shape assigned to a variable with an innocuous name, and it does not try —
    guessing at "long random-looking string" would redact commit SHAs, file
    hashes, and UUIDs, which is most of what a real command line contains.
    """
    if not text:
        return text

    result = text
    for pattern, group in PATTERNS:
        def replace(match, _group=group):
            if _group == 0:
                return _mask(match.group(0))
            value = match.group(_group)
            # Leave environment references and obvious non-secrets alone: a
            # command reading ${API_KEY} is exactly what you want to see.
            if _REFERENCE.match(value.strip()) or value.strip().lower() in _SAFE_VALUES:
                return match.group(0)
            prefix = match.group(0)[: match.start(_group) - match.start(0)]
            suffix = match.group(0)[match.end(_group) - match.start(0):]
            return prefix + _mask(value) + suffix

        result = pattern.sub(replace, result)
    return result


def redact_all(items) -> List[str]:
    return [redact(item) for item in items]
