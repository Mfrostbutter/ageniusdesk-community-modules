"""TokenPulse - an AgeniusDesk community module.

AI quota and budget observability across Anthropic, OpenAI, and OpenRouter:
plan/credit spend limits, credit balances, and operator-set monthly budget
gauges, loudest-first. Companion to the Model Cost module (per-model spend).
Admin keys never enter module code; every provider call goes through the
host-owned http.request bridge. See router.py; provider polling lives in
client.py, budget config in state.py.
"""

from .router import router  # noqa: F401
