"""
agent/graph.py

Wires all nodes into a LangGraph StateGraph.

Fix: switched from StateGraph(dict) to TypedDict with Annotated reducers.
LangGraph needs explicit reducer annotations to know how to merge state
between nodes — without them, fields not returned by a node revert to
their initial values on the next iteration.

Key reducers:
  messages        → operator.add  (accumulate, never replace)
  failed_searches → operator.add  (accumulate)
  everything else → default       (last-write wins)
"""

from __future__ import annotations
import operator
from dataclasses import fields
from typing import Any, Optional, Annotated, TYPE_CHECKING
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END

from agent.state import AgentState, StopReason, Hypothesis, InvestigationResult, Strategy
from agent.nodes.intake      import intake_node
from agent.nodes.investigate import investigate_node
from agent.nodes.tool_call   import tool_call_node
from agent.nodes.route       import route_node, route_node_decision
from agent.nodes.fallback    import fallback_node
from agent.nodes.output      import output_node

if TYPE_CHECKING:
    from classifier.schema import ClassifierOutput
    from indexer.index_schema import RepoIndex


# ---------------------------------------------------------------------------
# State schema — TypedDict with Annotated reducers
# LangGraph reads these annotations to know how to merge node outputs
# ---------------------------------------------------------------------------

class GraphState(TypedDict, total=False):
    # ── Inputs — set once, never change ───────────────────────────────
    classifier_output:  Any         # ClassifierOutput
    repo_index:         Any         # RepoIndex

    # ── Seed — set by INTAKE ───────────────────────────────────────────
    investigation_seed: str

    # ── Messages — ACCUMULATE (most important reducer) ─────────────────
    # operator.add means new messages are appended, not replaced
    messages: Annotated[list[dict], operator.add]

    # ── Hypothesis ─────────────────────────────────────────────────────
    hypothesis: Optional[Any]       # Hypothesis | None

    # ── Strategy ───────────────────────────────────────────────────────
    strategy: str

    # ── Control ────────────────────────────────────────────────────────
    iteration:          int
    max_iterations:     int
    context_chars_used: int
    max_context_chars:  int
    stop_reason:        Optional[str]

    # ── Tracking ───────────────────────────────────────────────────────
    # failed_searches accumulates across iterations
    failed_searches:    Annotated[list[str], operator.add]
    consecutive_empty:  int

    # ── Inter-node communication ───────────────────────────────────────
    _pending_tool:      Optional[str]
    _pending_args:      Optional[dict]
    _fallback_result:   Optional[dict]

    # ── Output ─────────────────────────────────────────────────────────
    result:             Optional[Any]   # InvestigationResult | None


# ---------------------------------------------------------------------------
# Node wrappers
# LangGraph passes GraphState dict → node returns partial dict to merge
# ---------------------------------------------------------------------------

def _wrap(node_fn):
    """
    Wrap a node function.
    Node receives full AgentState, returns dict of fields to update.
    LangGraph merges the returned dict into GraphState using reducers.
    """
    def wrapped(state_dict: GraphState) -> dict:
        state   = _to_agent_state(state_dict)
        updates = node_fn(state)
        return updates or {}
    wrapped.__name__ = node_fn.__name__
    return wrapped


def _route_wrapper(state_dict: GraphState) -> str:
    state = _to_agent_state(state_dict)
    return route_node_decision(state)


def _intake_exit(state_dict: GraphState) -> str:
    if state_dict.get("stop_reason") == StopReason.NEEDS_INPUT:
        return "output"
    return "investigate"


def _to_agent_state(d: GraphState) -> AgentState:
    """Reconstruct AgentState from GraphState dict."""
    state = AgentState.__new__(AgentState)
    for f in fields(AgentState):
        val = d.get(f.name)
        # Use field default if value is missing
        if val is None and f.name not in d:
            val = f.default if f.default is not f.default_factory else f.default_factory()
        setattr(state, f.name, val)
    return state


# ---------------------------------------------------------------------------
# Node output adapters
# Nodes return full message lists — we need to return only NEW messages
# so the operator.add reducer accumulates correctly
# ---------------------------------------------------------------------------

def _wrap_with_message_delta(node_fn):
    """
    Wrap nodes that update messages.
    Nodes return the full updated messages list.
    We convert to delta (new messages only) for operator.add reducer.
    """
    def wrapped(state_dict: GraphState) -> dict:
        state   = _to_agent_state(state_dict)
        old_len = len(state.messages)
        updates = node_fn(state) or {}

        if "messages" in updates:
            full_messages  = updates["messages"]
            # Only return new messages added by this node
            new_messages   = full_messages[old_len:]
            updates["messages"] = new_messages

        # Same for failed_searches — return only new entries
        if "failed_searches" in updates:
            old_failed = list(state.failed_searches or [])
            new_failed = updates["failed_searches"]
            # Return only newly added entries
            new_entries = [f for f in new_failed if f not in old_failed]
            updates["failed_searches"] = new_entries

        return updates

    wrapped.__name__ = node_fn.__name__
    return wrapped


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    graph = StateGraph(GraphState)

    # ── Add nodes ──────────────────────────────────────────────────────
    graph.add_node("intake",      _wrap_with_message_delta(intake_node))
    graph.add_node("investigate", _wrap_with_message_delta(investigate_node))
    graph.add_node("tool_call",   _wrap_with_message_delta(tool_call_node))
    graph.add_node("route",       _wrap(route_node))
    graph.add_node("fallback",    _wrap_with_message_delta(fallback_node))
    graph.add_node("output",      _wrap_with_message_delta(output_node))

    # ── Edges ──────────────────────────────────────────────────────────
    graph.add_edge(START, "intake")

    graph.add_conditional_edges("intake", _intake_exit, {
        "output":      "output",
        "investigate": "investigate",
    })

    graph.add_edge("investigate", "tool_call")
    graph.add_edge("tool_call",   "route")

    graph.add_conditional_edges("route", _route_wrapper, {
        "investigate": "investigate",
        "fallback":    "fallback",
        "output":      "output",
    })

    graph.add_edge("fallback", "output")
    graph.add_edge("output",   END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_investigation(
    classifier_output: "ClassifierOutput",
    repo_index:        "RepoIndex",
) -> AgentState:
    """
    Run the full investigator agent.
    Returns final AgentState with result populated.
    """
    initial_state: GraphState = {
        "classifier_output":  classifier_output,
        "repo_index":         repo_index,
        "investigation_seed": "",
        "messages":           [],
        "hypothesis":         None,
        "strategy":           Strategy.EXPLORE,
        "iteration":          0,
        "max_iterations":     5,
        "context_chars_used": 0,
        "max_context_chars":  24_000,
        "stop_reason":        None,
        "failed_searches":    [],
        "consecutive_empty":  0,
        "_pending_tool":      None,
        "_pending_args":      None,
        "_fallback_result":   None,
        "result":             None,
    }

    graph       = build_graph()
    final_dict  = graph.invoke(initial_state)
    return _to_agent_state(final_dict)
