"""Headless drive of the three v1 graphs with stub models + no-op tools.

Proves topology, the ReAct loop ending, the HITL interrupt + resume, and the
parallel fan-out, all without an API key or the host. Skips cleanly when the
langgraph extra is not installed (same posture as docker_mgr without aiodocker).
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.types import Command  # noqa: E402

# Import the graph builders by the package's real (hyphenated) directory name.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../agent-fleet
_PARENT = os.path.dirname(_ROOT)                                      # .../modules
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
_PKG = os.path.basename(_ROOT)

build_graph = importlib.import_module(f"{_PKG}.agent.graph").build_graph
build_fix_graph = importlib.import_module(f"{_PKG}.agent.graph_hitl").build_fix_graph
build_health_graph = importlib.import_module(f"{_PKG}.agent.graph_health").build_health_graph


class StubModel:
    """Minimal LangChain-chat-model stand-in: bind_tools is a no-op, ainvoke returns
    the next canned AIMessage. Never emits tool calls, so tool loops end immediately."""

    def __init__(self, replies):
        self._replies = list(replies)
        self._i = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return AIMessage(content=reply)


@tool
async def list_recent_errors(limit: int = 10) -> list:
    """Stub recent errors."""
    return [{"id": 1, "workflow_name": "wf", "error_message": "boom"}]


@tool
async def fleet_health() -> dict:
    """Stub fleet health."""
    return {"health_status": "healthy", "error_count_24h": 1}


@tool
async def list_executions(workflow_id: str = "", status: str = "", limit: int = 10) -> dict:
    """Stub executions."""
    return {"executions": [{"id": 9, "status": "error"}]}


STUB_TOOLS = [list_recent_errors, fleet_health, list_executions]
_CFG = {"configurable": {"thread_id": "t1"}}


def _last_text(state):
    for m in reversed(state["messages"]):
        if isinstance(m, AIMessage):
            return m.content
    return ""


async def test_ops_triage_runs_to_completion():
    graph = build_graph(StubModel(["## Triage: wf, root cause found"]), STUB_TOOLS)
    state = await graph.ainvoke({"messages": [("user", "triage")], "triage_target": "latest"})
    assert "Triage" in _last_text(state)


def test_ops_triage_topology():
    graph = build_graph(StubModel(["x"]), STUB_TOOLS)
    nodes = set(graph.get_graph().nodes.keys())
    assert {"agent", "tools"}.issubset(nodes)


async def test_fix_proposer_pauses_then_resumes():
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_fix_graph(StubModel(["diagnosis only", "## Proposed fix: do the thing"]),
                            STUB_TOOLS, checkpointer=MemorySaver())
    state = {"messages": [("user", "fix it")], "triage_target": "latest"}

    paused = False
    async for update in graph.astream(state, _CFG, stream_mode="updates"):
        if "__interrupt__" in update:
            paused = True
            break
    assert paused, "fix-proposer should HALT at the approval interrupt"

    final = None
    async for update in graph.astream(Command(resume={"action": "approve"}), _CFG, stream_mode="updates"):
        if "finalize" in update:
            final = update["finalize"]
    assert final is not None, "resume should run finalize to completion"


def test_fix_proposer_topology():
    graph = build_fix_graph(StubModel(["x"]), STUB_TOOLS)
    nodes = set(graph.get_graph().nodes.keys())
    assert {"investigate", "tools", "propose", "approval", "finalize"}.issubset(nodes)


async def test_health_reporter_fan_out():
    graph = build_health_graph(StubModel(["lens summary", "## Fleet health report\n- Overall: healthy"]),
                               STUB_TOOLS)
    state = await graph.ainvoke({"messages": [("user", "sweep")], "triage_target": "latest", "findings": []})
    # All three lenses wrote to the additive findings channel.
    assert len(state["findings"]) == 3
    assert "Fleet health report" in _last_text(state)


def test_health_reporter_topology():
    graph = build_health_graph(StubModel(["x"]), STUB_TOOLS)
    nodes = set(graph.get_graph().nodes.keys())
    assert {"plan", "lens_failures", "lens_health", "lens_volume", "synthesize"}.issubset(nodes)
