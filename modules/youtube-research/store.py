"""Persistent job store, backed by a module-private SQLite DB.

Research runs live in a `jobs` table in the module's own data dir
(`_data/jobs.db`) so the store works identically whether the module runs
in-process or in an isolated worker (which has no handle to the host DB). The row
carries the generated text too (breakdown, deep dive, transcript), so the UI
renders without a filesystem read; the vault holds the durable markdown
artifacts separately.

The data dir is AGD_MODULE_DATA_DIR when the host sets it (isolated subprocess),
else the in-tree module data dir. On first use the store migrates rows once from
the host's legacy `youtube_research_jobs` table (in_process installs only), so an
existing recent-list survives the move off the host DB.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get("AGD_MODULE_DATA_DIR") or "data/modules/youtube-research/_data") / "jobs.db"
_TABLE = "jobs"
_LEGACY_TABLE = "youtube_research_jobs"  # host dashboard.db table this replaces
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


async def _connect() -> aiosqlite.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(_DB_PATH))
    db.row_factory = aiosqlite.Row
    return db


_CREATE = f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
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


async def _ensure() -> None:
    global _ready
    if _ready:
        return
    db = await _connect()
    try:
        await db.execute(_CREATE)
        await db.execute(f"CREATE INDEX IF NOT EXISTS idx_jobs_created ON {_TABLE}(created_at DESC)")
        await db.commit()
        await _import_legacy(db)
    finally:
        await db.close()
    _ready = True


async def _import_legacy(db: aiosqlite.Connection) -> None:
    """One-time migration of existing rows from the host's legacy table.

    Only meaningful in_process (an isolated worker has no host DB handle and no
    prior data). Best-effort: a failure must never block the store from working.
    """
    from ._host import ISOLATED
    if ISOLATED:
        return
    cur = await db.execute(f"SELECT COUNT(*) AS n FROM {_TABLE}")
    row = await cur.fetchone()
    await cur.close()
    if row and row["n"]:
        return  # already populated; never re-import
    try:
        from backend.database import get_db
        host = await get_db()
        cur = await host.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{_LEGACY_TABLE}'"
        )
        exists = await cur.fetchone()
        await cur.close()
        if not exists:
            return
        cur = await host.execute(f"SELECT {', '.join(_COLS)} FROM {_LEGACY_TABLE}")
        rows = await cur.fetchall()
        await cur.close()
        placeholders = ", ".join("?" for _ in _COLS)
        for r in rows:
            await db.execute(
                f"INSERT OR IGNORE INTO {_TABLE} ({', '.join(_COLS)}) VALUES ({placeholders})",
                [r[c] for c in _COLS],
            )
        await db.commit()
        if rows:
            logger.info("youtube-research: migrated %d job(s) from the host legacy table", len(rows))
    except Exception as e:  # pragma: no cover - migration is best-effort
        logger.warning("youtube-research: legacy job import skipped (%s)", e)


async def create(job: dict[str, Any]) -> dict[str, Any]:
    await _ensure()
    cols = [c for c in _COLS if c in job]
    placeholders = ", ".join("?" for _ in cols)
    db = await _connect()
    try:
        await db.execute(
            f"INSERT INTO {_TABLE} ({', '.join(cols)}) VALUES ({placeholders})",
            [job[c] for c in cols],
        )
        await db.commit()
    finally:
        await db.close()
    return await get(job["id"])


async def update(job_id: str, **fields: Any) -> dict[str, Any] | None:
    await _ensure()
    fields = {k: v for k, v in fields.items() if k in _COLS and k != "id"}
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    db = await _connect()
    try:
        await db.execute(f"UPDATE {_TABLE} SET {sets} WHERE id = ?", [*fields.values(), job_id])
        await db.commit()
    finally:
        await db.close()
    return await get(job_id)


async def get(job_id: str) -> dict[str, Any] | None:
    await _ensure()
    db = await _connect()
    try:
        cur = await db.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        await cur.close()
    finally:
        await db.close()
    return dict(row) if row else None


async def find_by_video(video_id: str) -> dict[str, Any] | None:
    """Most-recent job for a video id, so a re-run can replace it (no dupes)."""
    if not video_id:
        return None
    await _ensure()
    db = await _connect()
    try:
        cur = await db.execute(
            f"SELECT * FROM {_TABLE} WHERE video_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (video_id,),
        )
        row = await cur.fetchone()
        await cur.close()
    finally:
        await db.close()
    return dict(row) if row else None


async def list_jobs(limit: int = 200) -> list[dict[str, Any]]:
    await _ensure()
    db = await _connect()
    try:
        cur = await db.execute(
            f"SELECT * FROM {_TABLE} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )
        rows = await cur.fetchall()
        await cur.close()
    finally:
        await db.close()
    return [{k: v for k, v in dict(r).items() if k not in _LIST_OMIT} for r in rows]


async def delete(job_id: str) -> bool:
    await _ensure()
    db = await _connect()
    try:
        cur = await db.execute(f"DELETE FROM {_TABLE} WHERE id = ?", (job_id,))
        await db.commit()
        n = cur.rowcount
    finally:
        await db.close()
    return n > 0


def public_list_item(job: dict[str, Any]) -> dict[str, Any]:
    """A job dict with the big text blobs stripped (for broadcasts)."""
    return {k: v for k, v in job.items() if k not in _LIST_OMIT}
