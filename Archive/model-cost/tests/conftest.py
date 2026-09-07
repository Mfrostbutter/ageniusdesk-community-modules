"""Test setup for the model-cost module.

The package directory is `model-cost` (a hyphen), which is not a valid Python
identifier, so it cannot be `import`ed by name. We register it under the alias
`model_cost` via importlib so tests can `from model_cost import client`.

AGD_BRIDGE_URL is set before the package loads so `_host` takes its ISOLATED
branch and never imports the AgeniusDesk host (`backend`) at collection time; the
tests mock `_host.http_request`/`http_grant` anyway.

Run with the ageniusdesk-ce environment (fastapi, httpx, pytest):
    python -m pytest modules/model-cost/tests -q
"""

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("AGD_BRIDGE_URL", "http://127.0.0.1:9/bridge")
os.environ.setdefault("AGD_BRIDGE_TOKEN", "test-token")

_PKG_DIR = Path(__file__).resolve().parents[1]

if "model_cost" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "model_cost", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["model_cost"] = module
    spec.loader.exec_module(module)
