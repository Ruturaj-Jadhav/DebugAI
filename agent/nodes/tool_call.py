"""
agent/nodes/tool_call.py

TOOL CALL node — deterministic, no LLM.
Executes whatever tool INVESTIGATE decided to call.

Responsibilities:
  1. Read _pending_tool and _pending_args from state
  2. Dispatch to the correct tool function
  3. Append result to messages
  4. Update failed_searches and consecutive_empty
  5. Update strategy signal for ROUTE node
"""

from agent.state import AgentState, StopReason
from agent.tools import dispatch_tool, is_empty_result


def tool_call_node(state: AgentState) -> dict:
    """
    LangGraph node function.
    Returns dict of updated fields.
    """
    tool_name = getattr(state, "_pending_tool", None)
    tool_args = getattr(state, "_pending_args", {}) or {}

    # No tool pending — INVESTIGATE said done or failed to pick one
    if not tool_name:
        print("[tool_call] No pending tool — skipping")
        return {}

    print(f"[tool_call] Executing: {tool_name}({tool_args})")

    # ── Execute tool ───────────────────────────────────────────────────
    result = dispatch_tool(tool_name, tool_args, state.repo_index)

    print(f"[tool_call] Result preview: {result[:120]}")

    # ── Detect empty result ────────────────────────────────────────────
    empty = is_empty_result(result)

    # Track failed searches for fallback + dedup signal
    updated_failed = list(state.failed_searches)
    if empty:
        # Record what failed — keyword or class name
        failed_key = (
            tool_args.get("keyword")
            or tool_args.get("class_name")
            or tool_name
        )
        if failed_key and failed_key not in updated_failed:
            updated_failed.append(failed_key)

    consecutive_empty = (state.consecutive_empty + 1) if empty else 0

    # ── Append result to messages ──────────────────────────────────────
    # Use role: "user" not "tool" — tool role requires tool_call_id
    # which varies by provider. user role works universally across all models.
    tool_message = f"[Tool result: {tool_name}]\n{result}"
    updated_messages = state.messages + [
        {"role": "user", "content": tool_message}
    ]

    return {
        "messages":         updated_messages,
        "failed_searches":  updated_failed,
        "consecutive_empty":consecutive_empty,
        "context_chars_used": state.context_chars_used + len(tool_message),
        "_pending_tool":    None,
        "_pending_args":    None,
    }
