"""Model Cost - an AgeniusDesk community module.

Per-model AI cost observability across Anthropic, OpenAI, and OpenRouter: which
models are costing what, with tokens and spend by model. Admin keys never enter
module code; every provider call goes through the host-owned http.request bridge
in every isolation mode. See router.py; provider polling + normalization live in
client.py, the derived-cost price table in prices.py.
"""

from .router import router  # noqa: F401
