"""
agent/nodes/output.py

OUTPUT node — final node in the graph.
Runs once. Produces InvestigationResult.

Two paths:
  1. Fallback path — _fallback_result already set by fallback_node
     Just assembles InvestigationResult from it.

  2. Direct path — confident/dead_end/needs_input stop
     LLM call to extract structured result from full investigation history.
"""

from __future__ import annotations
from ._llm_utils import call_llm, extract_json
from agent.state import AgentState, InvestigationResult, StopReason
from agent.prompts import build_output_prompt

_BASE_URL   = "https://openrouter.ai/api/v1"
_MODEL      = "openai/gpt-oss-120b:free"
_MAX_TOKENS = 1500
_TEMP       = 0


def output_node(state: AgentState) -> dict:
    """
    LangGraph node function.
    Always produces an InvestigationResult — never returns None.
    """
    stop_reason = state.effective_stop_reason()
    print(f"[output] Producing result. stop_reason={stop_reason}")

    # ── Path 1: Fallback result already prepared ───────────────────────
    fallback_data = getattr(state, "_fallback_result", None)
    if fallback_data:
        print("[output] Using fallback result")
        result = _assemble_result(fallback_data, state, used_raw_fallback=True)
        return {"result": result}

    # ── Path 2: Needs input — no investigation ran ─────────────────────
    if stop_reason == StopReason.NEEDS_INPUT:
        result = InvestigationResult(
            suspected_class   = "UNKNOWN",
            suspected_method  = "UNKNOWN",
            file_path         = "UNKNOWN",
            line_number       = None,
            call_chain        = [],
            confidence        = 0.0,
            confidence_reason = "Input too vague to investigate automatically.",
            technical_summary = "No stack trace, logs, endpoint, or class names found in input.",
            business_summary  = (
                "The description provided is too vague to investigate automatically. "
                "Please provide a stack trace, error logs, or a more specific description "
                "of which page and action is failing."
            ),
            stop_reason       = stop_reason,
            tools_used        = [],
            iterations        = state.iteration,
            used_raw_fallback = False,
        )
        return {"result": result}

    # ── Path 3: LLM extracts structured result from investigation ──────
    prompt = build_output_prompt(
        stop_reason       = stop_reason,
        hypothesis        = state.hypothesis,
        failed_searches   = state.failed_searches,
        classifier_output = state.classifier_output,
    )

    try:
        prompt_msg = {
            "role": "user",
            "content": prompt
        }
        raw    = call_llm(state.messages + [prompt_msg])
        parsed = extract_json(raw)
    except Exception as e:
        print(f"[output] LLM call failed: {e}")
        parsed = None

    if parsed is None:
        parsed = _emergency_fallback(state, stop_reason)

    result = _assemble_result(parsed, state, used_raw_fallback=False)
    return {"result": result}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _assemble_result(
    parsed:           dict,
    state:            AgentState,
    used_raw_fallback: bool,
) -> InvestigationResult:
    """Build InvestigationResult from parsed LLM dict + state context."""

    # Resolve file path from index if LLM returned UNKNOWN
    file_path = parsed.get("file_path", "UNKNOWN")
    suspected_class = parsed.get("suspected_class", "UNKNOWN")
    if file_path == "UNKNOWN" and suspected_class != "UNKNOWN":
        cls = state.repo_index.find_by_class_name(suspected_class)
        if cls:
            file_path = cls.file_path

    return InvestigationResult(
        suspected_class   = suspected_class,
        suspected_method  = parsed.get("suspected_method", "UNKNOWN"),
        file_path         = file_path,
        line_number       = parsed.get("line_number"),
        call_chain        = parsed.get("call_chain") or [],
        confidence        = float(parsed.get("confidence", 0.0)),
        confidence_reason = parsed.get("confidence_reason", ""),
        technical_summary = parsed.get("technical_summary", ""),
        business_summary  = parsed.get("business_summary", ""),
        stop_reason       = state.effective_stop_reason(),
        tools_used        = list(set(state.tools_called())),
        iterations        = state.iteration,
        used_raw_fallback = used_raw_fallback,
    )


def _emergency_fallback(state: AgentState, stop_reason: str) -> dict:
    """Last resort when OUTPUT LLM call also fails."""
    h = state.hypothesis
    return {
        "suspected_class":   h.suspected_class if h else "UNKNOWN",
        "suspected_method":  h.suspected_method if h else "UNKNOWN",
        "file_path":         "UNKNOWN",
        "line_number":       None,
        "call_chain":        [],
        "confidence":        h.confidence * 0.5 if h else 0.0,
        "confidence_reason": "Output extraction failed — reporting best hypothesis.",
        "technical_summary": h.reasoning if h else "Investigation did not produce a result.",
        "business_summary":  "The system encountered an error generating the report. Please try again.",
    }


