"""Persistent job store, backed by the host SQLite DB.

Research runs live in a `youtube_research_jobs` table in AgeniusDesk's
data/dashboard.db so the recent list survives restarts and is always visible in
the Research UI. The DB row carries the generated text too (breakdown, deep
dive, transcript), so the UI renders without a filesystem read; the vault holds
the durable markdown artifacts separately.

The table is created lazily (CREATE TABLE IF NOT EXISTS). Its name is distinct
from the host's retired `research_jobs` table, which the host drops on boot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.database import get_db

_TABLE = "youtube_research_jobs"
_COLS = (
    "id", "url", "video_id", "title", "channel", "duration_seconds", "depth",
    "engine", "engine_used", "destination", "model", "status", "progress",
    "error", "breakdown_md", "deepdive_md", "transcript_text", "artifact_dir",
    "created_at", "updated_at",
)
# Big text blobs omitted from list responses.
_LIST_OMIT = ("breakdown_md", "deepdive_md", "transcript_text")

_ready = False


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _ensure() -> None:
    global _ready
    if _ready:
        return
    db = await get_db()
    await db.execute(
        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
            id               TEXT PRIMARY KEY,
            url              TEXT NOT NULL DEFAULT '',
            video_id         TEXT NOT NULL DEFAULT '',
            title            TEXT NOT NULL DEFAULT '',
            channel          TEXT NOT NULL DEFAULT '',
            duration_seconds INTEGER,
            depth            TEXT NOT NULL DEFAULT 'single',
            engine           TEXT NOT NULL DEFAULT 'captions',
            engine_used      TEXT NOT NULL DEFAULT '',
            destination      TEXT NOT NULL DEFAULT '',
            model            TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'queued',
            progress         TEXT NOT NULL DEFAULT '',
            error            TEXT NOT NULL DEFAULT '',
            breakdown_md     TEXT NOT NULL DEFAULT '',
            deepdive_md      TEXT NOT NULL DEFAULT '',
            transcript_text  TEXT NOT NULL DEFAULT '',
            artifact_dir     TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL DEFAULT '',
            updated_at       TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute(f"CREATE INDEX IF NOT EXISTS idx_ytr_jobs_created ON {_TABLE}(created_at DESC)")
    await db.commit()
    _ready = True


async def create(job: dict[str, Any]) -> dict[str, Any]:
    await _ensure()
    cols = [c for c in _COLS if c in job]
    placeholders = ", ".join("?" for _ in cols)
    db = await get_db()
    await db.execute(
        f"INSERT INTO {_TABLE} ({', '.join(cols)}) VALUES ({placeholders})",
        [job[c] for c in cols],
    )
    await db.commit()
    return await get(job["id"])


async def update(job_id: str, **fields: Any) -> dict[str, Any] | None:
    await _ensure()
    fields = {k: v for k, v in fields.items() if k in _COLS and k != "id"}
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    db = await get_db()
    await db.execute(f"UPDATE {_TABLE} SET {sets} WHERE id = ?", [*fields.values(), job_id])
    await db.commit()
    return await get(job_id)


async def get(job_id: str) -> dict[str, Any] | None:
    await _ensure()
    db = await get_db()
    cur = await db.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def list_jobs(limit: int = 200) -> list[dict[str, Any]]:
    await _ensure()
    db = await get_db()
    cur = await db.execute(
        f"SELECT * FROM {_TABLE} ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (max(1, min(limit, 1000)),),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [{k: v for k, v in dict(r).items() if k not in _LIST_OMIT} for r in rows]


async def delete(job_id: str) -> bool:
    await _ensure()
    db = await get_db()
    cur = await db.execute(f"DELETE FROM {_TABLE} WHERE id = ?", (job_id,))
    await db.commit()
    return cur.rowcount > 0


def public_list_item(job: dict[str, Any]) -> dict[str, Any]:
    """A job dict with the big text blobs stripped (for broadcasts)."""
    return {k: v for k, v in job.items() if k not in _LIST_OMIT}
