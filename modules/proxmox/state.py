"""Module-private state: the self-guest declaration + read-only toggle (settings)
and a power-action audit log (audit), in a module-owned SQLite DB.

Lives in the module's own data dir (`AGD_MODULE_DATA_DIR` when the host sets it
under isolation, else the in-tree module data dir), so it works identically
in_process and sandboxed and never touches the host DB. Connection config
(base_url + token ref) is NOT here — that is the bridge's per-install endpoint
config; this store only holds what the bridge has no concept of: which guest is
the dashboard, whether the operator wants read-only, and who did what.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_DB_PATH = Path(os.environ.get("AGD_MODULE_DATA_DIR") or "data/modules/proxmox/_data") / "state.db"

_CREATE_SETTINGS = """CREATE TABLE IF NOT EXISTS settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    self_node  TEXT NOT NULL DEFAULT '',
    self_vmid  INTEGER,
    self_type  TEXT NOT NULL DEFAULT '',
    read_only  INTEGER NOT NULL DEFAULT 0
)"""

_CREATE_AUDIT = """CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT '',
    node    TEXT NOT NULL DEFAULT '',
    vmid    INTEGER,
    gtype   TEXT NOT NULL DEFAULT '',
    action  TEXT NOT NULL DEFAULT '',
    result  TEXT NOT NULL DEFAULT '',
    reason  TEXT NOT NULL DEFAULT ''
)"""

_ready = False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _connect() -> aiosqlite.Connection:
    global _ready
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(_DB_PATH))
    db.row_factory = aiosqlite.Row
    if not _ready:
        await db.execute(_CREATE_SETTINGS)
        await db.execute(_CREATE_AUDIT)
        await db.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        await db.commit()
        _ready = True
    return db


# ── settings (self-guest + read-only) ─────────────────────────────────────────


async def get_settings() -> dict[str, Any]:
    db = await _connect()
    try:
        cur = await db.execute("SELECT self_node, self_vmid, self_type, read_only FROM settings WHERE id = 1")
        row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        return {"self_node": "", "self_vmid": None, "self_type": "", "read_only": False}
    return {
        "self_node": row["self_node"] or "",
        "self_vmid": row["self_vmid"],
        "self_type": row["self_type"] or "",
        "read_only": bool(row["read_only"]),
    }


async def save_settings(
    *, self_node: str | None = None, self_vmid: int | None = None,
    self_type: str | None = None, read_only: bool | None = None,
) -> dict[str, Any]:
    current = await get_settings()
    merged = {
        "self_node": current["self_node"] if self_node is None else self_node,
        "self_vmid": current["self_vmid"] if self_vmid is None else self_vmid,
        "self_type": current["self_type"] if self_type is None else self_type,
        "read_only": current["read_only"] if read_only is None else bool(read_only),
    }
    db = await _connect()
    try:
        await db.execute(
            "UPDATE settings SET self_node = ?, self_vmid = ?, self_type = ?, read_only = ? WHERE id = 1",
            (merged["self_node"], merged["self_vmid"], merged["self_type"], 1 if merged["read_only"] else 0),
        )
        await db.commit()
    finally:
        await db.close()
    return merged


# ── audit (every power attempt, including refusals) ───────────────────────────


async def add_audit(
    *, actor: str, node: str, vmid: int | None, gtype: str, action: str, result: str, reason: str = "",
) -> None:
    db = await _connect()
    try:
        await db.execute(
            "INSERT INTO audit (ts, actor, node, vmid, gtype, action, result, reason) VALUES (?,?,?,?,?,?,?,?)",
            (_now(), actor, node, vmid, gtype, action, result, reason),
        )
        await db.commit()
    finally:
        await db.close()


async def list_audit(limit: int = 100) -> list[dict]:
    db = await _connect()
    try:
        cur = await db.execute(
            "SELECT ts, actor, node, vmid, gtype, action, result, reason FROM audit ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )
        rows = await cur.fetchall()
    finally:
        await db.close()
    return [dict(r) for r in rows]
