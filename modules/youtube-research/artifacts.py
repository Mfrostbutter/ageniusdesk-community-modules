"""Write research artifacts into the AgeniusDesk notes vault (the harness).

Mirrors the main app's research artifacts layout, but writes INTO the
containerized notes vault (data/workspace/research/...) through the host notes
API, so breakdowns are first-class, full-text-searchable notes rather than loose
files on the operator's machine.

Layout under research/:

    <topic-dest>/_youtube/<channel-slug>/<title-slug>[-<videoid>]/
        meta.json          video metadata + job params
        transcript.md      full transcript text
        BREAKDOWN.md       single-pass breakdown
        BREAKDOWN-deep.md  deep dive (only if run)

A blank destination files under DEFAULT_TOPIC ("_inbox"). Writes go through
notes.storage.write (indexed); directory ops use the vault path directly. Paths
returned to callers are vault-relative (e.g. "research/...").
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from backend.modules.notes import index as vault_index
from backend.modules.notes import storage as vault

logger = logging.getLogger(__name__)

RESEARCH_ROOT = "research"
DEFAULT_TOPIC = "_inbox"


def _research_abs() -> Path:
    return vault.VAULT_DIR / RESEARCH_ROOT


def _slug(text: str, max_len: int = 50) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:max_len].rstrip("-")) or "untitled"


_TITLE_CRUFT = re.compile(
    r"""\s*(?:
        [-|]\s*\d{4}\s*edition
      | [-|]\s*(?:full\s+)?tutorial
      | \([^)]*tutorial[^)]*\)
    )\s*$""",
    re.I | re.X,
)
_EMOJI = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]")
_ILLEGAL_SEG = re.compile(r'[<>:"|?*\x00-\x1f]')


def clean_title(title: str) -> str:
    """Normalize a YouTube title for storage + slugging (de-cruft, de-emoji, de-shout)."""
    t = _EMOJI.sub("", title or "")
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev = t
        t = _TITLE_CRUFT.sub("", t).strip()
    if t and not any(c.islower() for c in t):
        t = t.title()
    return t.strip()


def _safe_segment(name: str) -> str:
    name = _ILLEGAL_SEG.sub("", (name or "")).replace("/", "").replace("\\", "")
    name = name.strip().strip(". ")
    return "" if name in ("", ".", "..") else name


def sanitize_destination(destination: str) -> str:
    """Normalize a relative destination so it can never escape the research root."""
    parts = re.split(r"[\\/]+", destination or "")
    safe = [_safe_segment(p) for p in parts]
    return "/".join(p for p in safe if p)


def _topic_dest(destination: str) -> str:
    dest = sanitize_destination(destination)
    if not dest:
        return ""
    segs = dest.split("/")
    if "_youtube" in segs:
        segs = segs[: segs.index("_youtube")]
    return "/".join(segs)


def _dedupe_folder(parent_rel: Path, slug: str, video_id: str) -> str:
    target = _research_abs() / parent_rel / slug
    if not target.exists():
        return slug
    existing_id = ""
    meta = target / "meta.md"
    if meta.exists():
        try:
            existing_id = json.loads(meta.read_text(encoding="utf-8")).get("video_id") or ""
        except Exception:
            existing_id = ""
    if existing_id == (video_id or ""):
        return slug
    return f"{slug}-{video_id}" if video_id else slug


def artifact_dir_for(video_id: str, title: str, channel: str, destination: str = "") -> str:
    """Build the vault-relative artifact dir for one video (under research/)."""
    slug = _slug(clean_title(title) or title)
    base = Path("_youtube") / _slug(channel or "unknown-channel")
    topic = _topic_dest(destination) or DEFAULT_TOPIC
    parent = Path(topic) / base
    folder = _dedupe_folder(parent, slug, video_id)
    return (Path(RESEARCH_ROOT) / parent / folder).as_posix()


def transcript_markdown(meta: dict[str, Any], transcript_text: str) -> str:
    title = meta.get("title") or "(unknown title)"
    channel = meta.get("channel_title") or meta.get("channel") or "(unknown)"
    url = meta.get("url") or ""
    backend = (meta.get("transcript") or {}).get("backend") or meta.get("engine_used") or "captions"
    return (
        f"# Transcript: {title}\n\n"
        f"- Channel: {channel}\n- URL: {url}\n- Engine: {backend}\n\n"
        f"---\n\n{transcript_text}\n"
    )


async def write_base(rel_dir: str, *, meta: dict[str, Any], transcript_md: str, breakdown_md: str) -> str:
    """Write meta + transcript.md + BREAKDOWN.md into the vault. Returns rel_dir.

    The vault forces a .md extension, so metadata is stored as meta.md (JSON body).
    """
    await vault.write(f"{rel_dir}/meta.md", json.dumps(meta, indent=2, ensure_ascii=False))
    await vault.write(f"{rel_dir}/transcript.md", transcript_md)
    await vault.write(f"{rel_dir}/BREAKDOWN.md", breakdown_md)
    return rel_dir


async def write_deep(rel_dir: str, deep_md: str) -> None:
    await vault.write(f"{rel_dir}/BREAKDOWN-deep.md", deep_md)


# ── Folder picker (operates on the research/ subtree) ────────────────────────

def list_folders(rel: str = "") -> list[dict[str, Any]]:
    """Immediate subfolders of research/<rel> for the folder picker."""
    rel = sanitize_destination(rel)
    target = _research_abs() / rel if rel else _research_abs()
    if not target.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted((p for p in target.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
        if child.name.startswith(".") or child.name == "_youtube":
            continue
        child_rel = f"{rel}/{child.name}" if rel else child.name
        has_children = any(
            g.is_dir() and not g.name.startswith(".") and g.name != "_youtube" for g in child.iterdir()
        )
        out.append({"name": child.name, "path": sanitize_destination(child_rel), "has_children": has_children})
    return out


def make_folder(rel: str) -> str:
    """Create research/<rel> (parents ok). Returns the sanitized relative path."""
    rel = sanitize_destination(rel)
    if not rel:
        raise ValueError("empty folder path")
    (_research_abs() / rel).mkdir(parents=True, exist_ok=True)
    return rel


_STARTER_TOPICS = (
    DEFAULT_TOPIC,
    "ai-engineering",
    "automation-and-n8n",
    "engineering-and-devtools",
    "business-and-marketing",
    "productivity",
    "misc",
)


def ensure_taxonomy() -> None:
    """Seed research/ with the inbox + a small starter taxonomy (idempotent)."""
    vault.ensure_vault()
    base = _research_abs()
    for topic in _STARTER_TOPICS:
        (base / topic).mkdir(parents=True, exist_ok=True)


def list_topics() -> list[str]:
    """Existing research topic folders (the classifier candidate set), minus inbox."""
    return [f["name"] for f in list_folders("") if f["name"] != DEFAULT_TOPIC]


async def move_artifact(current_rel: str, destination: str) -> str:
    """Move an existing artifact folder to a new destination. Returns new rel dir."""
    src = (vault.VAULT_DIR / current_rel).resolve()
    research = _research_abs().resolve()
    if research not in src.parents or not src.is_dir():
        raise ValueError("source artifact folder not found")

    meta_path = src / "meta.md"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    video_id = meta.get("video_id") or ""
    title = meta.get("title") or src.name
    channel = meta.get("channel_title") or meta.get("channel") or ""

    new_rel = artifact_dir_for(video_id, title, channel, destination)
    dst = (vault.VAULT_DIR / new_rel).resolve()
    if dst == src:
        return new_rel
    if dst.exists():
        raise ValueError("a different video already occupies the target folder")

    # Re-write each file at the new location through the indexed API, then drop
    # the old copies (write + delete == an indexed move).
    for f in sorted(src.glob("*")):
        if not f.is_file():
            continue
        content = f.read_text(encoding="utf-8")
        await vault.write(f"{new_rel}/{f.name}", content)
        old_rel = f"{current_rel}/{f.name}"
        try:
            f.unlink()
            if f.name.endswith(".md"):
                await vault_index.remove_note(old_rel)
        except Exception as e:  # pragma: no cover
            logger.warning("move: failed to remove %s: %s", old_rel, e)
    _prune_empty_parents(src)
    return new_rel


def _prune_empty_parents(start: Path) -> None:
    base = _research_abs().resolve()
    cur = start.resolve()
    while cur != base and base in cur.parents:
        try:
            if any(cur.iterdir()):
                break
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent
