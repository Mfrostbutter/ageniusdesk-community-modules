"""Entrypoint for `langgraph dev` / LangGraph Studio.

Best-effort developer tool. The PRIMARY live view is the in-app graph view inside
AgeniusDesk; this just lets a developer open the graphs in Studio. The dashboard
NEVER imports this module: its runner builds the graphs itself with in-process
tools and a lazily resolved key.

langgraph.json loads this by FILE PATH, so the module is imported standalone (no
package context). It bootstraps sys.path and imports the graph builders by the
package's real (possibly hyphenated) directory name via importlib, so the builders'
relative imports (from .prompts import ...) resolve correctly.

Needs in env (langgraph.json loads .env): ANTHROPIC_API_KEY (or ANTHROPIC_KEY), and
optionally LANGSMITH_* for tracing. The n8n tools call backend.* and only work
inside AgeniusDesk; in Studio they surface as tool errors, so use Studio to inspect
topology + structure and run the agents for real from the dashboard.
"""

from __future__ import annotations

import importlib
import os
import sys

# Don't try to ship traces without a key (avoids noisy 401s); lights up the moment
# a key exists.
if os.environ.get("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes") and not os.environ.get("LANGSMITH_API_KEY"):
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

# langchain-anthropic reads ANTHROPIC_API_KEY; accept ANTHROPIC_KEY too.
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_KEY"]

# Make the module importable as a package by its real directory name so the graph
# builders' relative imports resolve when this file is loaded standalone.
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))   # .../agent-fleet/agent
_MOD_ROOT = os.path.dirname(_AGENT_DIR)                    # .../agent-fleet
_PARENT = os.path.dirname(_MOD_ROOT)                       # .../modules
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
_PKG = os.path.basename(_MOD_ROOT)                         # "agent-fleet"

_graph_mod = importlib.import_module(f"{_PKG}.agent.graph")
_hitl_mod = importlib.import_module(f"{_PKG}.agent.graph_hitl")
_health_mod = importlib.import_module(f"{_PKG}.agent.graph_health")
_tools_mod = importlib.import_module(f"{_PKG}.tools_local")

from langchain_anthropic import ChatAnthropic  # noqa: E402

_llm = ChatAnthropic(
    model=os.environ.get("OPS_TRIAGE_MODEL", "claude-sonnet-4-6"),
    temperature=0,
    max_tokens=int(os.environ.get("OPS_TRIAGE_MAX_TOKENS", "2048")),
)

TOOLS = _tools_mod.TOOLS

# ops_triage: the classic ReAct tool loop.
graph = _graph_mod.build_graph(_llm, TOOLS)

# fix_proposer: human-in-the-loop. The dev server supplies the checkpointer, so its
# interrupt() pauses are inspectable + resumable right in Studio.
fix_proposer = _hitl_mod.build_fix_graph(_llm, TOOLS)

# health_reporter: parallel fan-out (3 lenses) -> synthesize.
health_reporter = _health_mod.build_health_graph(_llm, TOOLS)
