"""
agent/nodes/investigate.py

INVESTIGATE node — the LLM reasoning turn.
Runs every iteration of the loop.

Responsibilities:
  1. Build per-turn prompt based on current strategy + hypothesis
  2. Call LLM with full message history
  3. Parse LLM response → extract tool decision + updated hypothesis
  4. Update state: hypothesis, messages, iteration counter

The LLM reads the full messages history every turn.
This is how it self-corrects, avoids repeating tool calls,
and tracks its own reasoning — no separate deduplication needed.
"""

from __future__ import annotations
from ._llm_utils import call_llm, extract_json
from agent.state import AgentState, Hypothesis, Strategy, StopReason
from agent.prompts import build_investigate_prompt

_BASE_URL   = "https://openrouter.ai/api/v1"
_MODEL      = "openai/gpt-oss-120b:free"
_MAX_TOKENS = 1500
_TEMP       = 0


def investigate_node(state: AgentState) -> dict:
    """
    LangGraph node function.
    Returns dict of updated fields.
    """
    print(f"[investigate] {state.status_line()}")

    # ── Build per-turn instruction ─────────────────────────────────────
    turn_prompt = build_investigate_prompt(
        strategy       = state.strategy,
        hypothesis     = state.hypothesis,
        failed_searches= state.failed_searches,
        iteration      = state.iteration,
        max_iterations = state.max_iterations,
    )

    # ── Call LLM ──────────────────────────────────────────────────────
    messages_with_turn = state.messages + [
        {"role": "user", "content": turn_prompt}
    ]

    try:
        raw = call_llm(messages_with_turn)
    except Exception as e:
        print(f"[investigate] LLM call failed: {e}")
        return {
            "stop_reason": StopReason.DEAD_END,
            "iteration":   state.iteration + 1,
        }

    # ── Parse response ─────────────────────────────────────────────────
    parsed = extract_json(raw)
    if parsed is None:
        print(f"[investigate] Could not parse LLM response — skipping turn")
        return {"iteration": state.iteration + 1}

    # ── Extract hypothesis ─────────────────────────────────────────────
    hypothesis = _extract_hypothesis(parsed)

    # ── Extract action ─────────────────────────────────────────────────
    action      = parsed.get("action", "call_tool")
    stop_reason = parsed.get("stop_reason")

    # LLM said it's done
    if action == "done" and stop_reason in (StopReason.CONFIDENT, StopReason.DEAD_END):
        print(f"[investigate] LLM signalled done: {stop_reason}")
        updated_messages = state.messages + [
            {"role": "user",      "content": turn_prompt},
            {"role": "assistant", "content": raw},
        ]
        return {
            "hypothesis":          hypothesis,
            "stop_reason":         stop_reason,
            "iteration":           state.iteration + 1,
            "messages":            updated_messages,
            "context_chars_used":  state.context_chars_used + len(turn_prompt) + len(raw),
        }

    # ── Extract tool call ──────────────────────────────────────────────
    tool_name = parsed.get("tool")
    tool_args = parsed.get("args", {})

    # Append assistant response to messages
    updated_messages = state.messages + [
        {"role": "user",      "content": turn_prompt},
        {"role": "assistant", "content": raw},
    ]

    print(f"[investigate] tool={tool_name} args={tool_args}")
    if hypothesis:
        print(f"[investigate] hypothesis={hypothesis.suspected_class}.{hypothesis.suspected_method}() @ {hypothesis.confidence:.0%}")

    return {
        "hypothesis":         hypothesis,
        "messages":           updated_messages,
        "iteration":          state.iteration + 1,
        "context_chars_used": state.context_chars_used + len(turn_prompt) + len(raw),
        # tool_name and tool_args passed via state for tool_call node
        "_pending_tool":      tool_name,
        "_pending_args":      tool_args,
    }


def _extract_hypothesis(parsed: dict) -> Hypothesis | None:
    """Build Hypothesis from parsed LLM response."""
    h = parsed.get("hypothesis")
    if not h:
        return None

    try:
        return Hypothesis(
            suspected_class  = h.get("suspected_class"),
            suspected_method = h.get("suspected_method"),
            reasoning        = h.get("reasoning", ""),
            confidence       = float(h.get("confidence", 0.0)),
            evidence         = h.get("evidence") or [],
            refuted_classes  = h.get("refuted_classes") or [],
            missing          = h.get("missing"),
        )
    except Exception as e:
        print(f"[investigate] Hypothesis parse error: {e}")
        return None
