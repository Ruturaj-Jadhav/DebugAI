"""
agent/nodes/route.py

ROUTE node — pure logic, no LLM.
Decides what happens after each tool call.

Responsibilities:
  1. Check stop conditions (stop_reason set, max_iter, context_limit)
  2. Update strategy based on hypothesis strength and consecutive_empty
  3. Detect dead end — nothing found after exhausting searches
  4. Return routing decision for LangGraph conditional edges
"""

from agent.state import AgentState, Strategy, StopReason

# How many consecutive empty results before forcing strategy switch
_EMPTY_THRESHOLD = 2

# How many failed searches before declaring dead end
_DEAD_END_THRESHOLD = 4


def route_node(state: AgentState) -> dict:
    """
    LangGraph node function.
    Returns updated strategy and stop_reason.
    """
    updates = {}

    # ── 1. Hard stop conditions ────────────────────────────────────────
    if state.stop_reason:
        print(f"[route] Stop reason already set: {state.stop_reason}")
        return {}

    if state.is_over_budget():
        print(f"[route] Context limit reached ({state.context_chars_used}/{state.max_context_chars})")
        return {"stop_reason": StopReason.CONTEXT_LIMIT}

    if state.iteration >= state.max_iterations:
        print(f"[route] Max iterations reached ({state.iteration}/{state.max_iterations})")
        return {"stop_reason": StopReason.MAX_ITER}

    # ── 2. Dead end detection ──────────────────────────────────────────
    if len(state.failed_searches) >= _DEAD_END_THRESHOLD:
        # Only declare dead end if hypothesis is still very weak
        if state.hypothesis is None or state.hypothesis.confidence < 0.3:
            print(f"[route] Dead end — {len(state.failed_searches)} failed searches, no hypothesis")
            return {"stop_reason": StopReason.DEAD_END}

    # ── 3. Confident stop ─────────────────────────────────────────────
    if state.hypothesis and state.hypothesis.is_confident():
        print(f"[route] Hypothesis confident ({state.hypothesis.confidence:.0%}) → stopping")
        return {"stop_reason": StopReason.CONFIDENT}

    # ── 4. Strategy update ─────────────────────────────────────────────
    new_strategy = _decide_strategy(state)
    if new_strategy != state.strategy:
        print(f"[route] Strategy: {state.strategy} → {new_strategy}")
        updates["strategy"] = new_strategy

    print(f"[route] Continue. iter={state.iteration}/{state.max_iterations} strategy={new_strategy or state.strategy}")
    return updates


def route_node_decision(state: AgentState) -> str:
    """
    LangGraph conditional edge function.
    Returns the name of the next node.
    Called by LangGraph after route_node runs.
    """
    if state.stop_reason in (StopReason.MAX_ITER, StopReason.CONTEXT_LIMIT):
        return "fallback"
    if state.stop_reason in (StopReason.CONFIDENT, StopReason.DEAD_END, StopReason.NEEDS_INPUT):
        return "output"
    return "investigate"


# ---------------------------------------------------------------------------
# Strategy decision
# ---------------------------------------------------------------------------

def _decide_strategy(state: AgentState) -> str:
    """
    Decide explore vs exploit based on current evidence.

    EXPLOIT when:
      - Hypothesis is strong (confidence >= 0.5, has evidence)
      - We have a suspected class to dig into

    EXPLORE when:
      - No hypothesis yet
      - Hypothesis is weak (confidence < 0.5)
      - Stuck — consecutive empty results >= threshold
    """
    # Stuck in wrong direction → force explore
    if state.consecutive_empty >= _EMPTY_THRESHOLD:
        print(f"[route] Stuck ({state.consecutive_empty} empty results) → switching to EXPLORE")
        return Strategy.EXPLORE

    # Strong hypothesis → exploit
    if state.hypothesis and state.hypothesis.is_strong():
        return Strategy.EXPLOIT

    # Weak or no hypothesis → explore
    return Strategy.EXPLORE
