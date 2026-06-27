"""Deep dive: one transcript-grounded extraction of actionable technical depth.

Reads the RAW transcript (not the shallow breakdown) so verbatim numbers,
command sequences, and quotes survive. Output lands in BREAKDOWN-deep.md.
"""

from __future__ import annotations

from .llm import LLMError, complete
from .prompts import DEEP_DIVE_SYSTEM, deep_dive_prompt

# The deep dive aims for real depth, well past the single-pass default.
# complete() clamps this down adaptively if the model rejects it.
DEEP_DIVE_MAX_TOKENS = 16000


async def run(title: str, channel: str, url: str, transcript: str, *, model: str = "") -> str:
    """Extract deep technical detail from the transcript. Returns markdown."""
    if not (transcript or "").strip():
        raise LLMError("Deep dive needs a non-empty transcript.")
    return await complete(
        DEEP_DIVE_SYSTEM,
        deep_dive_prompt(title, channel, url, transcript),
        max_tokens=DEEP_DIVE_MAX_TOKENS,
        model=model,
    )
