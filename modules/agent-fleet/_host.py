"""Host-mode detection.

Agent Fleet runs in_process (Option A): its tools call `backend.*` directly and it
resolves the LLM key host-side, so it is an in_process-only module for v1. This flag
exists so the router can skip importing the host auth gate under isolation (where
`backend` is not importable). Under isolation the module loads but cannot run a graph
(no host, no tools, no key); see the README and the spec.
"""

from __future__ import annotations

import os

ISOLATED = bool(os.environ.get("AGD_BRIDGE_URL"))
