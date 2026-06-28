"""Write research artifacts into the AgeniusDesk notes vault (the harness).

Mirrors the main app's research artifacts layout, but writes INTO the notes vault
(data/workspace/research/...) through the host services facade (`_host`), so
breakdowns are first-class, full-text-searchable notes. All vault access goes
through `_host`: bridge calls when isolated, direct host calls in_process. This
module works only in vault-relative paths ("research/...") and never touches the
host filesystem directly.

Layout under research/:

    <topic-dest>/<channel-slug>/<title-slug>[-<videoid>]/
        meta.md            video metadata + job params (JSON body)
        transcript.md      full transcript text
        BREAKDOWN.md       single-pass breakdown
        BREAKDOWN-deep.md  deep dive (only if run)

A blank destination files under DEFAULT_TOPIC ("_inbox"). Paths returned to
callers are vault-relative (e.g. "research/...").
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import _host

logger = logging.getLogger(__name__)

RESEARCH_ROOT = "research"
DEFAULT_TOPIC = "_inbox"


def _rel(*parts: str) -> str:
    """Join into a vault-relative path under the research root."""
    tail = "/".join(p.strip("/") for p in parts if p)
    return f"{RESEARCH_ROOT}/{tail}" if tail else RESEARCH_ROOT


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
    return sanitize_destination(destination)


async def _meta_video_id(rel_dir: str) -> str | None:
    """video_id from a folder's meta.md, or None if the folder/meta is absent."""
    raw = await _host.notes_read(f"{rel_dir}/meta.md")
    if raw is None:
        return None
    try:
        return json.loads(raw).get("video_id") or ""
    except Exception:
        return ""


async def _dedupe_folder(parent_rel: str, slug: str, video_id: str) -> str:
    """Pick a folder name under parent_rel, disambiguating only a DIFFERENT video."""
    existing_id = await _meta_video_id(_rel(parent_rel, slug))
    if existing_id is None or existing_id == (video_id or ""):
        return slug
    return f"{slug}-{video_id}" if video_id else slug


async def artifact_dir_for(video_id: str, title: str, channel: str, destination: str = "") -> str:
    """Build the vault-relative artifact dir for one video.

    Layout: research/<topic>/<channel-slug>/<title-slug>[-<videoid>]/
    """
    slug = _slug(clean_title(title) or title)
    topic = _topic_dest(destination) or DEFAULT_TOPIC
    parent = (Path(topic) / _slug(channel or "unknown-channel")).as_posix()
    folder = await _dedupe_folder(parent, slug, video_id)
    return _rel(parent, folder)


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
    await _host.notes_write(f"{rel_dir}/meta.md", json.dumps(meta, indent=2, ensure_ascii=False))
    await _host.notes_write(f"{rel_dir}/transcript.md", transcript_md)
    await _host.notes_write(f"{rel_dir}/BREAKDOWN.md", breakdown_md)
    return rel_dir


async def write_deep(rel_dir: str, deep_md: str) -> None:
    await _host.notes_write(f"{rel_dir}/BREAKDOWN-deep.md", deep_md)


# ── Folder picker (operates on the research/ subtree) ────────────────────────


async def list_folders(rel: str = "") -> list[dict[str, Any]]:
    """Immediate subfolders of research/<rel> for the folder picker."""
    rel = sanitize_destination(rel)
    names = await _host.notes_list_folders(_rel(rel))
    out: list[dict[str, Any]] = []
    for name in names:
        if name.startswith(".") or name == "_youtube":
            continue
        child_rel = f"{rel}/{name}" if rel else name
        sub = await _host.notes_list_folders(_rel(child_rel))
        has_children = any(s != "_youtube" and not s.startswith(".") for s in sub)
        out.append({"name": name, "path": sanitize_destination(child_rel), "has_children": has_children})
    return sorted(out, key=lambda f: f["name"].lower())


async def make_folder(rel: str) -> str:
    """Create research/<rel> (parents ok). Returns the sanitized relative path."""
    rel = sanitize_destination(rel)
    if not rel:
        raise ValueError("empty folder path")
    await _host.notes_make_folder(_rel(rel))
    return rel


_STARTER_TOPICS = (
    DEFAULT_TOPIC,
    "ai-assisted-coding",
    "ai-engineering",
    "automation-and-n8n",
    "engineering-and-devtools",
    "business-and-marketing",
    "productivity",
    "misc",
)


async def ensure_taxonomy() -> None:
    """Seed research/ with the inbox + a small starter taxonomy (idempotent)."""
    await _host.ensure_research_root()
    for topic in _STARTER_TOPICS:
        await _host.notes_make_folder(_rel(topic))


async def list_topics() -> list[str]:
    """Existing research topic folders (the classifier candidate set), minus inbox."""
    return [f["name"] for f in await list_folders("") if f["name"] != DEFAULT_TOPIC]


async def move_artifact(current_rel: str, destination: str) -> str:
    """Move an existing artifact folder to a new destination. Returns new rel dir."""
    current_rel = current_rel.strip("/")
    if not current_rel.startswith(RESEARCH_ROOT + "/"):
        raise ValueError("source artifact folder not found")

    meta_raw = await _host.notes_read(f"{current_rel}/meta.md")
    meta: dict[str, Any] = {}
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except Exception:
            meta = {}
    video_id = meta.get("video_id") or ""
    title = meta.get("title") or current_rel.rsplit("/", 1)[-1]
    channel = meta.get("channel_title") or meta.get("channel") or ""

    new_rel = await artifact_dir_for(video_id, title, channel, destination)
    if new_rel == current_rel:
        return new_rel

    # Only a DIFFERENT video at the target is a real conflict; the same video is a
    # duplicate re-run (fall through and overwrite, then drop the source copy).
    existing_id = await _meta_video_id(new_rel)
    if existing_id and video_id and existing_id != video_id:
        raise ValueError("a different video already occupies the target folder")

    files = await _host.notes_list_files(current_rel)
    if not files:
        raise ValueError("source artifact folder not found")
    for name in files:
        await _host.notes_move(f"{current_rel}/{name}", f"{new_rel}/{name}")
    await _prune_empty_parents(current_rel)
    return new_rel


async def _prune_empty_parents(start_rel: str) -> None:
    """Remove now-empty folders from start_rel up to (not including) the research
    root. No-op under isolation (the bridge has no rmdir)."""
    cur = start_rel.strip("/")
    while cur and cur != RESEARCH_ROOT and cur.startswith(RESEARCH_ROOT + "/"):
        await _host.remove_empty_dir(cur)
        cur = cur.rsplit("/", 1)[0]
