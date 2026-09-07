"""Test setup for the tokenpulse module.

AGD_BRIDGE_URL is set before the package loads so `_host` takes its ISOLATED
branch (no `backend` import at collection time); the tests mock
`_host.http_request`/`http_grant`. AGD_MODULE_DATA_DIR points state.py at a temp
dir so budget writes never touch the repo. `modules/` is on sys.path so
`import tokenpulse` resolves to this package.

Run with the ageniusdesk-ce environment (fastapi, httpx, pytest):
    python -m pytest modules/tokenpulse/tests -q
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AGD_BRIDGE_URL", "http://127.0.0.1:9/bridge")
os.environ.setdefault("AGD_BRIDGE_TOKEN", "test-token")
os.environ.setdefault("AGD_MODULE_DATA_DIR", tempfile.mkdtemp(prefix="tp-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
