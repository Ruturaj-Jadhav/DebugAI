"""
agent/nodes/intake.py

INTAKE node — first node in the graph.
Runs once. Sets up AgentState for the entire investigation.

Responsibilities:
  1. Decide max_iterations based on mode
  2. Build investigation_seed from ClassifierOutput (or raw_input if failed)
  3. Check if input is too vague → set stop_reason = needs_input early
  4. Seed messages with system prompt + initial context
  5. Set initial strategy = EXPLORE always (start broad)

Does NOT make any LLM calls.
Does NOT query the index.
Pure setup — fast and deterministic.
"""

from agent.state import AgentState, Strategy, StopReason
from agent.prompts import SYSTEM_PROMPT


# Confidence threshold below which we won't investigate
_MIN_CONFIDENCE_TO_PROCEED = 0.25


def intake_node(state: AgentState) -> dict:
    """
    LangGraph node function.
    Receives AgentState, returns dict of fields to update.
    """
    co = state.classifier_output

    # ── 1. Max iterations based on mode ───────────────────────────────
    # Business mode starts from zero — needs more iterations to navigate
    # Developer mode has frames as starting point — fewer needed
    max_iterations = 8 if co.mode == "business" else 5

    # ── 2. Check if too vague to proceed ──────────────────────────────
    # Classifier failure takes priority over vague check
    is_too_vague = (
        co.classifier_succeeded and  # only check vagueness if classifier ran
        co.confidence < _MIN_CONFIDENCE_TO_PROCEED
        and not co.evidence.frames
        and not co.evidence.logs
        and not co.intent.endpoint
    )

    if is_too_vague:
        seed = _build_vague_seed(co)
        messages = _seed_messages(seed)
        print(f"[intake] Input too vague (confidence={co.confidence:.2f}) → needs_input")
        return {
            "investigation_seed": seed,
            "messages":           messages,
            "max_iterations":     max_iterations,
            "strategy":           Strategy.EXPLORE,
            "stop_reason":        StopReason.NEEDS_INPUT,
        }

    # ── 3. Build investigation seed ────────────────────────────────────
    if not co.classifier_succeeded:
        seed = _build_raw_seed(co)
        print(f"[intake] Classifier failed — seeding from raw_input")
    else:
        seed = _build_structured_seed(co)
        print(f"[intake] Structured seed built (mode={co.mode}, confidence={co.confidence:.2f})")

    # ── 4. Seed messages ───────────────────────────────────────────────
    messages = _seed_messages(seed)

    print(f"[intake] max_iterations={max_iterations} strategy={Strategy.EXPLORE}")
    print(f"[intake] seed preview: {seed[:120]}...")

    return {
        "investigation_seed": seed,
        "messages":           messages,
        "max_iterations":     max_iterations,
        "strategy":           Strategy.EXPLORE,
        "stop_reason":        None,
        "iteration":          0,
        "context_chars_used": sum(len(m["content"]) for m in messages),
    }


# ---------------------------------------------------------------------------
# Seed builders
# ---------------------------------------------------------------------------

def _build_structured_seed(co) -> str:
    """
    Build investigation seed from structured ClassifierOutput.
    Gives the LLM the most relevant signals in priority order.
    """
    lines = ["== DEBUGGING SESSION START ==\n"]

    # Mode
    lines.append(f"Mode: {co.mode}")

    # Primary symptom — always present
    lines.append(f"Symptom: {co.failure.symptom}")

    # Error information
    if co.failure.error_type:
        msg = f" — {co.failure.error_message}" if co.failure.error_message else ""
        lines.append(f"Error: {co.failure.error_type}{msg}")

    if co.failure.http_status:
        lines.append(f"HTTP Status: {co.failure.http_status}")

    # Intent signals
    if co.intent.endpoint:
        lines.append(f"Endpoint: {co.intent.endpoint}")
    if co.intent.feature:
        lines.append(f"Feature: {co.intent.feature}")
    if co.intent.action:
        lines.append(f"Action: {co.intent.action}")
    if co.intent.entity_ids:
        lines.append(f"Entity IDs: {', '.join(co.intent.entity_ids)}")

    # Stack frames — strongest developer signal
    user_frames = co.evidence.user_frames()
    if user_frames:
        lines.append(f"\nStack frames (entry → failure):")
        for f in user_frames:
            lines.append(f"  {f.display()}")

    # Root cause from Caused by chain
    if co.cause and co.cause.error_type:
        msg = f": {co.cause.error_message}" if co.cause.error_message else ""
        lines.append(f"\nRoot cause: {co.cause.error_type}{msg}")

    # Log hints
    if co.evidence.logs:
        lines.append(f"\nKey log lines:")
        for log in co.evidence.logs[:3]:
            lines.append(f"  [{log.level or '?'}] {log.raw}")

    # Additional context from user
    if co.additional_context:
        lines.append(f"\nAdditional context: {co.additional_context}")

    lines.append(
        "\nBegin investigation. Use the tools to find the root cause. "
        "Start with search_index if you have no stack frames, "
        "or get_class_summary if you already have class names from the frames."
    )

    return "\n".join(lines)


def _build_raw_seed(co) -> str:
    """
    Fallback seed when classifier failed.
    Uses raw_input directly — LLM extracts what it can.
    """
    return (
        "== DEBUGGING SESSION START ==\n\n"
        "Note: Automatic extraction failed. "
        "Reading raw user input directly.\n\n"
        f"Raw input:\n{co.raw_input}\n\n"
        "Extract any class names, method names, error types, or feature names "
        "from the text above. Then begin investigation using the tools."
    )


def _build_vague_seed(co) -> str:
    """
    Seed for vague inputs. Investigation won't proceed but
    we need something to show in the output.
    """
    return (
        "== DEBUGGING SESSION START ==\n\n"
        f"Input received: {co.failure.symptom or co.raw_input}\n\n"
        "This input is too vague to investigate automatically. "
        "No stack trace, logs, endpoint, or class names were found."
    )


def _seed_messages(seed: str) -> list[dict]:
    """
    Build the initial messages list.
    System prompt + seed as first user message.
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": seed},
    ]
