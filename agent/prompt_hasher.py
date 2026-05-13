"""Prompt content hashing utility.

Used by the orchestrator to log a short, stable identity for every prompt
file sent to the LLM. Discovery Agent can then:

1. Detect prompt drift between runs (hash changes ⇒ prompt content changed).
2. Correlate a Git commit that modified a prompt file with a behavioural
   change observed in the trace.
3. Know which prompt version was active for a given run, without bloating
   the trace with the full prompt text.

We deliberately do NOT log the prompt body itself — only its file name
and a truncated SHA-256 digest of its content.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"

_HASH_PREFIX = "sha256:"
_HASH_LEN = 16  # short prefix is plenty for change-detection without bloat


@lru_cache(maxsize=64)
def hash_prompt(filename: str) -> str:
    """Return `sha256:<16-hex-chars>` for the named prompt file.

    Cached per process so we don't re-read disk on every LLM turn.
    Returns ``"<missing>"`` if the file does not exist rather than raising,
    so a typo in a layer prompt name never crashes a run.
    """
    path = PROMPTS_DIR / filename
    if not path.exists():
        return "<missing>"
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()[:_HASH_LEN]
    return f"{_HASH_PREFIX}{digest}"


def hash_text(text: str) -> str:
    """Hash an arbitrary string the same way as `hash_prompt`."""
    if not text:
        return "<empty>"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{_HASH_PREFIX}{digest}"
