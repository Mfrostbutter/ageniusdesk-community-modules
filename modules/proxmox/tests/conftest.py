"""Test setup: make `import proxmox` resolve from the tests dir.

AGD_BRIDGE_URL and AGD_MODULE_DATA_DIR must be set in the ENVIRONMENT before this
package is imported (pytest imports the `proxmox` package while resolving this
conftest, which is earlier than this file's body). The test command sets them:

    AGD_BRIDGE_URL=http://127.0.0.1:9/x AGD_MODULE_DATA_DIR=<tmp> \
      uv run --project ../ageniusdesk-ce --with pytest --with respx \
      python -m pytest modules/proxmox/tests -q

AGD_BRIDGE_URL routes router.py down its ISOLATED branch so it does not import the
AgeniusDesk host (`backend`) at collection time; AGD_MODULE_DATA_DIR keeps state.db
in a temp dir. This file only ensures `modules/` is importable as a fallback.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
