"""Module-private budget config: the operator's monthly USD budget per provider.

A small JSON file in the module's own data dir (`AGD_MODULE_DATA_DIR` under
isolation, else the in-tree data dir), so it works identically in_process and
sandboxed and never touches the host DB. Connection config (base URLs, secret
refs) is NOT here; that is the bridge's per-install endpoint config. This holds
only what the bridge has no concept of: the monthly budget the operator wants to
gauge actual spend against.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_DATA_DIR = Path(os.environ.get("AGD_MODULE_DATA_DIR") or "data/modules/tokenpulse/_data")
_FILE = _DATA_DIR / "settings.json"
_lock = threading.Lock()

PROVIDERS = ("anthropic", "openai", "openrouter")


def _load() -> dict[str, Any]:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_FILE)


def get_budgets() -> dict[str, float]:
    """Monthly USD budget per provider (0 = no gauge)."""
    with _lock:
        raw = _load().get("budgets", {})
    out: dict[str, float] = {}
    for p in PROVIDERS:
        try:
            out[p] = max(0.0, float(raw.get(p, 0) or 0))
        except (TypeError, ValueError):
            out[p] = 0.0
    return out


def set_budgets(budgets: dict[str, Any]) -> dict[str, float]:
    """Merge operator-supplied budgets (per provider, USD/month). Unknown keys
    ignored; a non-number or negative clears that provider's budget."""
    with _lock:
        data = _load()
        current = dict(data.get("budgets", {}))
        for p in PROVIDERS:
            if p not in budgets:
                continue
            try:
                v = float(budgets[p])
                current[p] = v if v > 0 else 0.0
            except (TypeError, ValueError):
                current[p] = 0.0
        data["budgets"] = current
        _save(data)
    return get_budgets()
