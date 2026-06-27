"""Intake, classify, and auto-file a breakdown into the notes vault.

Flow (spec 6.1):
  1. Intake to research/inbox/ first, so nothing is lost if classification fails.
  2. Classify + tag with the LLM, constrained to the EXISTING research/ topic
     folders (the model never invents a topic).
  3. Auto-file: move the note to research/<topic>/ and write the tags into the
     frontmatter. If no confident fit, it stays in research/inbox/.

Filing goes through the host notes vault (backend.modules.notes), so breakdowns
become first-class, FTS-indexed notes rather than loose files. The capability
surface is the single declared vault subtree `research/`. (Writes that go
through the indexed notes API are not visible to the static scanner, so the
declared filesystem write_path shows as an over-declaration - that is expected.)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from backend.modules.notes import index as vault_index
from backend.modules.notes import storage as vault

from .llm import LLMError, complete
from .prompts import CLASSIFY_SYSTEM, classify_prompt

logger = logging.getLogger(__name__)

RESEARCH_ROOT = "research"
INBOX = f"{RESEARCH_ROOT}/inbox"
CONFIDENCE_THRESHOLD = 0.55

# Scaffolded starter taxonomy (spec 6.2): generic, operator-editable folders so
# classification has targets out of the box. "inbox" is never a target.
STARTER_TOPICS = [
    "ai-and-llms",
    "automation-and-n8n",
    "business-and-marketing",
    "engineering-and-devtools",
    "productivity",
    "misc",
]


def _slug(text: str, max_len: int = 60) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "untitled"


def ensure_taxonomy() -> None:
    """Seed the research/ subtree with the starter topic folders (idempotent)."""
    vault.ensure_vault()
    base = vault.VAULT_DIR / RESEARCH_ROOT
    (base / "inbox").mkdir(parents=True, exist_ok=True)
    for topic in STARTER_TOPICS:
        (base / topic).mkdir(parents=True, exist_ok=True)


def list_topics() -> list[str]:
    """The existing research/ subfolders (the classifier's candidate set), minus inbox."""
    base = vault.VAULT_DIR / RESEARCH_ROOT
    if not base.is_dir():
        return []
    return [
        p.name
        for p in sorted(base.iterdir(), key=lambda x: x.name.lower())
        if p.is_dir() and not p.name.startswith(".") and p.name != "inbox"
    ]


def _compose(meta: dict, tags: list[str], topic: str, breakdown_md: str) -> str:
    """Markdown note: YAML frontmatter + the breakdown body."""
    fm: list[str] = ["---"]
    fm.append(f"title: {json.dumps(meta.get('title') or '', ensure_ascii=False)}")
    if meta.get("channel"):
        fm.append(f"channel: {json.dumps(meta['channel'], ensure_ascii=False)}")
    if meta.get("url"):
        fm.append(f"url: {meta['url']}")
    if meta.get("video_id"):
        fm.append(f"video_id: {meta['video_id']}")
    fm.append("source: youtube-research")
    if topic:
        fm.append(f"topic: {topic}")
    if tags:
        fm.append("tags: [" + ", ".join(_slug(t, 40) for t in tags if t) + "]")
    fm.append(f"filed: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    fm.append("---")
    header = f"# {meta.get('title') or 'Untitled'}\n"
    sub = []
    if meta.get("channel"):
        sub.append(f"Channel: {meta['channel']}")
    if meta.get("url"):
        sub.append(meta["url"])
    subline = ("\n" + " · ".join(sub) + "\n") if sub else ""
    return "\n".join(fm) + "\n\n" + header + subline + "\n" + breakdown_md.strip() + "\n"


async def _delete(rel: str) -> None:
    """Remove a note from disk and the FTS index (write+delete == move)."""
    try:
        vp = vault.resolve(rel)
        if vp.abs.exists():
            vp.abs.unlink()
        await vault_index.remove_note(rel)
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("filing: failed to remove %s: %s", rel, e)


def _parse_classification(raw: str) -> dict:
    """Pull the {topic, tags, confidence} JSON out of an LLM response."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text).rstrip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"topic": "", "tags": [], "confidence": 0.0}
    topic = (data.get("topic") or "").strip()
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()][:6]
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {"topic": topic, "tags": tags, "confidence": confidence}


async def intake(breakdown_md: str, meta: dict) -> str:
    """Write the breakdown to research/inbox/ first. Returns the note path."""
    ensure_taxonomy()
    name = _slug(meta.get("title") or meta.get("video_id") or "untitled")
    rel = f"{INBOX}/{name}.md"
    await vault.write(rel, _compose(meta, [], "", breakdown_md))
    return rel


async def classify_and_file(inbox_rel: str, breakdown_md: str, meta: dict, *, model: str = "") -> dict:
    """Classify into an existing topic and move the note there, or keep it in inbox.

    Returns {filed (rel path), topic, tags, confidence}.
    """
    topics = list_topics()
    classification = {"topic": "", "tags": [], "confidence": 0.0}
    try:
        raw = await complete(
            CLASSIFY_SYSTEM,
            classify_prompt(meta.get("title", ""), meta.get("channel", ""), breakdown_md, topics),
            max_tokens=400,
            model=model,
        )
        classification = _parse_classification(raw)
    except LLMError as e:
        logger.warning("filing: classification failed, keeping in inbox: %s", e)

    topic = classification["topic"]
    tags = classification["tags"]
    confidence = classification["confidence"]

    confident = topic in topics and confidence >= CONFIDENCE_THRESHOLD
    if confident:
        dest_rel = f"{RESEARCH_ROOT}/{topic}/{inbox_rel.rsplit('/', 1)[-1]}"
        await vault.write(dest_rel, _compose(meta, tags, topic, breakdown_md))
        await _delete(inbox_rel)
        return {"filed": dest_rel, "topic": topic, "tags": tags, "confidence": confidence}

    # No confident fit: keep in inbox, but write the tags we did get.
    await vault.write(inbox_rel, _compose(meta, tags, "", breakdown_md))
    return {"filed": inbox_rel, "topic": "", "tags": tags, "confidence": confidence}
