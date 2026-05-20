"""
agent/nodes/fallback.py

RAW FALLBACK node — triggered when max_iter or context_limit reached.
NOT a blind guess. Receives everything the agent learned.

Smart fallback input:
  - raw_input (original user report)
  - best hypothesis reached (even if confidence was low)
  - evidence gathered
  - failed paths (what didn't work)
  - stack frames from classifier

Produces a reasoned best-effort answer clearly marked as uncertain.
Sets used_raw_fallback=True on the result so caller knows.
"""

from __future__ import annotations
from ._llm_utils import call_llm, extract_json
from agent.state import AgentState, StopReason
from agent.prompts import build_fallback_prompt

_BASE_URL   = "https://openrouter.ai/api/v1"
_MODEL      = "openai/gpt-oss-120b:free"
_MAX_TOKENS = 1500
_TEMP       = 0


def fallback_node(state: AgentState) -> dict:
    """
    LangGraph node function.
    Produces best-effort InvestigationResult using all available context.
    """
    print(f"[fallback] Running smart fallback (stop_reason={state.effective_stop_reason()})")

    prompt = build_fallback_prompt(
        raw_input         = state.classifier_output.raw_input,
        hypothesis        = state.hypothesis,
        failed_searches   = state.failed_searches,
        tools_called      = state.tools_called(),
        classifier_output = state.classifier_output,
    )

    try:
        raw = call_llm([{"role": "user", "content": prompt}])
        parsed = extract_json(raw)
    except Exception as e:
        print(f"[fallback] LLM call failed: {e}")
        parsed = None

    if parsed is None:
        # Ultimate fallback — build from hypothesis if we have one
        parsed = _build_from_hypothesis(state)

    # Mark as fallback result
    parsed["used_raw_fallback"] = True
    parsed["stop_reason"] = state.effective_stop_reason()

    return {"_fallback_result": parsed}


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm(prompt: str) -> str:
    client = OpenAI(
        base_url = _BASE_URL,
        api_key  = os.environ.get("OPENROUTER_API_KEY", ""),
    )
    completion = client.chat.completions.create(
        model       = _MODEL,
        temperature = _TEMP,
        max_tokens  = _MAX_TOKENS,
        stream      = True,
        messages    = [{"role": "user", "content": prompt}],
    )
    chunks = []
    for chunk in completion:
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            chunks.append(delta.content)
    return "".join(chunks)


def _parse_response(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("{"):
        json_str = text
    else:
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            json_str = fence.group(1)
        else:
            brace = re.search(r"\{.*\}", text, re.DOTALL)
            json_str = brace.group(0) if brace else None

    if not json_str:
        return None
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _build_from_hypothesis(state: AgentState) -> dict:
    """Last resort — build result dict directly from state hypothesis."""
    h = state.hypothesis
    if h and h.suspected_class:
        cls = state.repo_index.find_by_class_name(h.suspected_class)
        file_path = cls.file_path if cls else "UNKNOWN"
        return {
            "suspected_class":   h.suspected_class,
            "suspected_method":  h.suspected_method or "UNKNOWN",
            "file_path":         file_path,
            "line_number":       None,
            "call_chain":        [],
            "confidence":        h.confidence * 0.7,  # discount — not LLM confirmed
            "confidence_reason": f"Best guess from partial investigation. {h.reasoning}",
            "technical_summary": f"Investigation pointed to {h.suspected_class}.{h.suspected_method}() but could not confirm. Evidence: {'; '.join(h.evidence)}",
            "business_summary":  f"The system identified a likely location for the issue but could not fully confirm it. Manual review recommended.",
        }
    return {
        "suspected_class":   "UNKNOWN",
        "suspected_method":  "UNKNOWN",
        "file_path":         "UNKNOWN",
        "line_number":       None,
        "call_chain":        [],
        "confidence":        0.1,
        "confidence_reason": "Investigation could not identify a suspect location.",
        "technical_summary": f"Investigation exhausted without finding the bug. Failed searches: {', '.join(state.failed_searches)}",
        "business_summary":  "The system could not automatically locate the bug. Please provide more details such as logs or a stack trace.",
    }
