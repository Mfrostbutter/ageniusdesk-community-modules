"""Host-services facade: dual-mode (isolated subprocess vs in-process).

A community module cannot import the AgeniusDesk host when it runs in an
out-of-process worker (the `backend` import is blocked in the sandbox). This is
the single place that knows the difference:

  - When AGD_BRIDGE_URL is set (the host spawned us in an isolated subprocess),
    every privileged action goes through the loopback host capability bridge
    (`/api/_host/*`), authed by the per-spawn AGD_BRIDGE_TOKEN.
  - Otherwise (a default in_process install) it calls the host packages directly.

The direct branch imports `backend.*`; AgeniusDesk's module scanner flags those
as host-import HIGH, which is accurate: in_process there is no sandbox, so the
module really does reach host internals. Under isolation this file makes only
HTTP calls to 127.0.0.1 and imports nothing from the host.

Every host op is async here (the bridge path is HTTP); callers await uniformly.
"""

from __future__ import annotations

import os

import httpx

ISOLATED = bool(os.environ.get("AGD_BRIDGE_URL"))
_BRIDGE_URL = os.environ.get("AGD_BRIDGE_URL", "").rstrip("/")
_BRIDGE_TOKEN = os.environ.get("AGD_BRIDGE_TOKEN", "")
_TIMEOUT = 300.0


class HostError(RuntimeError):
    """A host bridge / host-call failure with an operator-facing message."""


# ── bridge transport (isolated mode) ──────────────────────────────────────────


async def _bridge(path: str, payload: dict) -> httpx.Response:
    headers = {"authorization": f"Bearer {_BRIDGE_TOKEN}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        return await c.post(f"{_BRIDGE_URL}{path}", json=payload, headers=headers)


def _raise_for(resp: httpx.Response, action: str) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = resp.text[:200]
        raise HostError(f"{action} failed (HTTP {resp.status_code}): {detail}")


# ── notes namespace ───────────────────────────────────────────────────────────


async def notes_write(rel: str, content: str) -> dict:
    if ISOLATED:
        r = await _bridge("/api/_host/notes/write", {"path": rel, "content": content})
        _raise_for(r, "notes.write")
        return r.json()
    from backend.modules.notes import storage as vault
    return await vault.write(rel, content)


async def notes_read(rel: str) -> str | None:
    """Note content, or None if it does not exist."""
    if ISOLATED:
        r = await _bridge("/api/_host/notes/read", {"path": rel})
        if r.status_code == 404:
            return None
        _raise_for(r, "notes.read")
        return r.json().get("content")
    from backend.modules.notes import storage as vault
    try:
        return vault.read(rel)
    except FileNotFoundError:
        return None


async def notes_move(src: str, dst: str) -> None:
    """Move a note (write dst, archive src). Missing src is a no-op."""
    if ISOLATED:
        r = await _bridge("/api/_host/notes/move", {"src": src, "dst": dst})
        if r.status_code == 404:
            return
        _raise_for(r, "notes.move")
        return
    from backend.modules.notes import storage as vault
    try:
        content = vault.read(src)
    except FileNotFoundError:
        return
    await vault.write(dst, content)
    try:
        await vault.archive(src)
    except FileNotFoundError:
        pass


async def notes_make_folder(rel: str) -> None:
    if ISOLATED:
        r = await _bridge("/api/_host/notes/make-folder", {"rel": rel})
        _raise_for(r, "notes.make_folder")
        return
    from backend.modules.notes import storage as vault
    (vault.VAULT_DIR / rel).mkdir(parents=True, exist_ok=True)


async def notes_list_folders(rel: str) -> list[str]:
    if ISOLATED:
        r = await _bridge("/api/_host/notes/list-folders", {"rel": rel})
        if r.status_code == 404:
            return []
        _raise_for(r, "notes.list_folders")
        return r.json().get("folders", [])
    from backend.modules.notes import storage as vault
    target = vault.VAULT_DIR / rel
    if not target.is_dir():
        return []
    return sorted(
        c.name for c in target.iterdir() if c.is_dir() and not c.is_symlink() and not c.name.startswith(".")
    )


async def notes_list_files(rel: str) -> list[str]:
    if ISOLATED:
        r = await _bridge("/api/_host/notes/list-files", {"rel": rel})
        if r.status_code == 404:
            return []
        _raise_for(r, "notes.list_files")
        return r.json().get("files", [])
    from backend.modules.notes import storage as vault
    target = vault.VAULT_DIR / rel
    if not target.is_dir():
        return []
    return sorted(
        c.name for c in target.iterdir() if c.is_file() and not c.is_symlink() and not c.name.startswith(".")
    )


async def remove_empty_dir(rel: str) -> None:
    """Prune an empty directory (cosmetic cleanup after a move). No-op under
    isolation: the bridge has no rmdir, and leaving an empty folder is harmless."""
    if ISOLATED:
        return
    from backend.modules.notes import storage as vault
    target = vault.VAULT_DIR / rel
    try:
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
    except OSError:
        pass


async def ensure_research_root() -> None:
    """Ensure the vault + research/ exist. The host ensures the vault at startup
    under isolation; in_process we ensure it ourselves."""
    if not ISOLATED:
        from backend.modules.notes import storage as vault
        vault.ensure_vault()
    await notes_make_folder("research")


# ── assistant namespace (isolated mode only; in_process uses llm.py directly) ──


async def assistant_complete(system: str, user: str, *, model: str = "", max_tokens: int = 8000) -> str:
    r = await _bridge(
        "/api/_host/assistant/complete",
        {"system": system, "user": user, "model": model, "max_tokens": max_tokens},
    )
    _raise_for(r, "assistant.complete")
    return r.json().get("text", "")
