"""Auto-classify a breakdown into a research topic folder.

When a run lands in the inbox (no explicit destination), the LLM picks the best
existing topic folder, or proposes a concise new one when none fit. The caller
then moves the artifact there. Constrained to avoid taxonomy fragmentation: it
prefers existing folders and only invents a topic when warranted.
"""

from __future__ import annotations

import json
import logging
import re

from .llm import LLMError, complete
from .prompts import CLASSIFY_SYSTEM, classify_prompt

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.45


def _slug(text: str, max_len: int = 40) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")


def _parse(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text).rstrip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"topic": "", "confidence": 0.0}
    topic = _slug(str(data.get("topic") or ""))
    if topic in ("", "inbox", "_inbox"):
        return {"topic": "", "confidence": 0.0}
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {"topic": topic, "confidence": confidence}


async def classify_topic(title: str, channel: str, breakdown: str, topics: list[str], *, model: str = "") -> str:
    """Return a topic folder slug to file under, or "" to leave in the inbox."""
    try:
        raw = await complete(
            CLASSIFY_SYSTEM,
            classify_prompt(title, channel, breakdown, topics),
            max_tokens=200,
            model=model,
        )
    except LLMError as e:
        logger.warning("classify: %s", e)
        return ""
    result = _parse(raw)
    if result["topic"] and result["confidence"] >= CONFIDENCE_THRESHOLD:
        return result["topic"]
    return ""
