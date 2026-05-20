"""
agent/prompts.py

All LLM prompt templates for the investigator agent.
Keeping prompts in one file makes iteration fast — no hunting across nodes.

Three prompt types:
  SYSTEM          → base instructions, tool definitions, reasoning rules
  INVESTIGATE     → per-turn prompt, changes based on strategy (explore/exploit)
  OUTPUT          → final structured result extraction
  FALLBACK        → smart fallback when agent runs out of time
"""

from agent.tools import AVAILABLE_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_tools() -> str:
    """Format tool definitions for the system prompt."""
    lines = []
    for name, info in AVAILABLE_TOOLS.items():
        lines.append(f"\n{name}")
        lines.append(f"  Description: {info['description']}")
        args = ", ".join(f"{k}: {v}" for k, v in info["args"].items())
        lines.append(f"  Args: {{{args}}}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt — sent once, stays in messages throughout
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are an expert Java debugging investigator.
Your job is to find the root cause of bugs in Java Spring Boot applications.

You have access to a structural index of the codebase and three tools to investigate it.

════════════════════════════════════════
AVAILABLE TOOLS
════════════════════════════════════════
{_format_tools()}

════════════════════════════════════════
HOW TO RESPOND EACH TURN
════════════════════════════════════════

Every turn you must respond with a JSON object. No other text. No markdown.

{{
  "thought": "your reasoning about current hypothesis and what to do next",
  "hypothesis": {{
    "suspected_class":  "ClassName or null",
    "suspected_method": "methodName or null",
    "reasoning":        "why you suspect this location",
    "confidence":       0.0,
    "evidence":         ["fact 1", "fact 2"],
    "refuted_classes":  ["RuledOutClass"],
    "missing":          "what you still need to confirm or null"
  }},
  "action": "call_tool" | "done",
  "tool":   "search_index" | "get_class_summary" | "read_method",
  "args":   {{"arg_name": "arg_value"}},
  "stop_reason": null | "confident" | "dead_end"
}}

RULES:
- If action is "done" → set stop_reason to "confident" or "dead_end"
- If action is "call_tool" → stop_reason must be null
- tool and args are required when action is "call_tool"
- hypothesis must always be present and updated each turn
- confidence is a float between 0.0 and 1.0
- evidence is a list of concrete facts you observed, not assumptions
- Never call the same tool with the same args twice
- Never include class names in evidence that you have not seen in tool results

════════════════════════════════════════
REASONING FRAMEWORK
════════════════════════════════════════

Each turn ask yourself:
  1. What is my current hypothesis? How strong is the evidence?
  2. What is the weakest part of my hypothesis?
  3. Which tool addresses that weakness?
  4. Have I called this tool with these args before? (check history)

CRITICAL COMMITMENT RULES — follow these exactly:
- If the stack trace names a class (e.g. OwnerController) → set suspected_class to that class IMMEDIATELY.
  Do NOT search for the class name — go straight to get_class_summary on it.
- If you have called get_class_summary on a class → you MUST set suspected_class in your hypothesis.
  A class summary is enough evidence to commit. Confidence should be >= 0.4 after a summary.
- suspected_class must NEVER stay null after you have seen a class summary.
- Do NOT raise confidence without setting suspected_class first. Confidence without a class is meaningless.
- Prefer class names from stack frames over keywords when choosing what to search.

════════════════════════════════════════
CONFIDENCE GUIDE
════════════════════════════════════════

0.0 - 0.3  No hypothesis yet — keep exploring
0.3 - 0.5  Weak hypothesis — need more evidence
0.5 - 0.75 Strong hypothesis — exploit specific class/method
0.75 - 1.0 Confident — can stop and report

Stop when confidence >= 0.75 AND you have read actual method code.
Do not stop based on class summaries alone.

STOP RULES — STRICTLY ENFORCED:
- You MAY NOT set action: done unless you have called read_method at least once.
- You MAY NOT claim confident unless suspected_class AND suspected_method are both set.
- If iterations are running out and you have not read any method → call read_method NOW.
- If you cannot reach confidence 0.75 → set action: done with stop_reason: dead_end.
  A honest dead_end is better than a fabricated confident.
"""


# ---------------------------------------------------------------------------
# INVESTIGATE prompt — changes per turn based on strategy
# ---------------------------------------------------------------------------

def build_investigate_prompt(
    strategy:         str,
    hypothesis:       object,   # Hypothesis | None
    failed_searches:  list[str],
    iteration:        int,
    max_iterations:   int,
) -> str:
    """
    Per-turn instruction injected before LLM decides next action.
    Shapes behaviour based on current strategy and hypothesis state.
    """
    remaining = max_iterations - iteration

    # ── Hypothesis section ───────────────────────────────────────────
    if hypothesis is None:
        hyp_section = "Current hypothesis: NONE — you have not formed one yet."
    else:
        hyp_section = f"""Current hypothesis:
  Suspected: {hypothesis.suspected_class or '?'}.{hypothesis.suspected_method or '?'}()
  Confidence: {hypothesis.confidence:.0%}
  Evidence: {', '.join(hypothesis.evidence) if hypothesis.evidence else 'none yet'}
  Ruled out: {', '.join(hypothesis.refuted_classes) if hypothesis.refuted_classes else 'none'}
  Missing: {hypothesis.missing or 'nothing identified'}"""

    # ── Failed searches section ──────────────────────────────────────
    if failed_searches:
        failed_section = (
            f"Dead ends (do NOT retry these): {', '.join(failed_searches)}"
        )
    else:
        failed_section = "No dead ends yet."

    # ── Strategy instruction ─────────────────────────────────────────
    if strategy == "explore":
        strategy_instruction = """CURRENT MODE: EXPLORE
Your hypothesis is weak or absent. Search broadly to find candidates.
- Use search_index with different keywords
- Look at the feature name, action, endpoint, or error type
- Once you find 2+ candidate classes, switch to exploiting the most likely one
- Do NOT read method bodies yet — get class summaries first"""

    else:  # exploit
        suspected = hypothesis.suspected_class if hypothesis else "unknown"
        strategy_instruction = f"""CURRENT MODE: EXPLOIT
You have a strong candidate: {suspected}
- Use get_class_summary to understand its methods if not done yet
- Use read_method on the most suspicious methods
- Build concrete evidence from actual code
- If reading the code confirms your hypothesis → set action: done
- If reading the code refutes it → update hypothesis and consider exploring again"""

    # ── Urgency section ──────────────────────────────────────────────
    if remaining <= 2:
        has_read = any(
            "[Tool result: read_method]" in m.get("content", "")
            for m in []   # checked externally via state — safe default here
        )
        urgency = f"""⚠️ ONLY {remaining} ITERATION(S) REMAINING.
Priority order:
  1. If you have NOT called read_method yet → call read_method on your best suspect NOW.
  2. If you have called read_method AND have a hypothesis → set action: done.
  3. If you have found nothing useful → set action: done, stop_reason: dead_end.
Do NOT claim confident without having read actual method code.
Do NOT invent evidence you have not seen in tool results."""
    else:
        urgency = f"Iterations remaining: {remaining}"

    return f"""
{hyp_section}

{failed_section}

{strategy_instruction}

{urgency}

Respond with JSON only. No other text.
"""


# ---------------------------------------------------------------------------
# OUTPUT prompt — extracts final structured result from investigation
# ---------------------------------------------------------------------------

def build_output_prompt(
    stop_reason:      str,
    hypothesis:       object,   # Hypothesis | None
    failed_searches:  list[str],
    classifier_output: object,  # ClassifierOutput
) -> str:
    """
    Prompt for the OUTPUT node.
    Shapes the response based on why the agent stopped.
    """

    hyp_text = (
        hypothesis.summary()
        if hypothesis
        else "No hypothesis was formed."
    )

    stop_context = {
        "confident":     "You have high confidence. Report your findings clearly.",
        "dead_end":      "You could not find the bug in the index. Be honest about this.",
        "max_iter":      "You ran out of iterations. Report your best guess with caveats.",
        "context_limit": "Context limit reached. Report based on what you found so far.",
        "needs_input":   "The input was too vague. Ask for more specific information.",
    }.get(stop_reason, "Report your best findings.")

    return f"""You are writing the final debugging report.

Investigation summary:
{hyp_text}

Failed searches: {', '.join(failed_searches) if failed_searches else 'none'}
Stop reason: {stop_reason} — {stop_context}

Original symptom: {classifier_output.failure.symptom}
Error type: {classifier_output.failure.error_type or 'unknown'}
Mode: {classifier_output.mode}

Produce a JSON object with this exact schema:
{{
  "suspected_class":   "ClassName or UNKNOWN",
  "suspected_method":  "methodName or UNKNOWN",
  "file_path":         "absolute path or UNKNOWN",
  "line_number":       123 or null,
  "call_chain":        ["EntryClass.method", "SuspectClass.method"],
  "confidence":        0.0,
  "confidence_reason": "one sentence explaining confidence level",
  "technical_summary": "2-3 sentences for a developer",
  "business_summary":  "1-2 sentences in plain English for a business user",
  "stop_reason":       "{stop_reason}"
}}

RULES:
- Never invent class names or line numbers not seen in tool results
- If stop_reason is dead_end or needs_input → suspected_class = "UNKNOWN"
- business_summary must not contain class names or technical jargon
- confidence must match stop_reason:
    confident     → 0.75 – 1.0
    max_iter      → 0.3  – 0.74
    context_limit → 0.3  – 0.74
    dead_end      → 0.0  – 0.2
    needs_input   → 0.0

Return ONLY valid JSON. No markdown. No explanation.
"""


# ---------------------------------------------------------------------------
# FALLBACK prompt — smart fallback with full investigation context
# ---------------------------------------------------------------------------

def build_fallback_prompt(
    raw_input:        str,
    hypothesis:       object,   # Hypothesis | None
    failed_searches:  list[str],
    tools_called:     list[str],
    classifier_output: object,  # ClassifierOutput
) -> str:
    """
    Smart fallback — not a blind guess.
    Receives everything the agent learned, even if confidence was low.
    Used when: max_iter hit OR context_limit reached.
    """

    hyp_text = (
        hypothesis.summary()
        if hypothesis
        else "No hypothesis was formed during investigation."
    )

    frames_text = ""
    if classifier_output.evidence.frames:
        frame_strs = [f.display() for f in classifier_output.evidence.frames]
        frames_text = f"Stack frames: {' → '.join(frame_strs)}"

    cause_text = ""
    if classifier_output.cause:
        cause_text = (
            f"Root cause: {classifier_output.cause.error_type} "
            f"— {classifier_output.cause.error_message}"
        )

    return f"""The investigation has ended without full confidence.
Use everything available to produce the most informed assessment possible.

════════ ORIGINAL USER REPORT ════════
{raw_input}

════════ WHAT WAS EXTRACTED ════════
Symptom : {classifier_output.failure.symptom}
Error   : {classifier_output.failure.error_type or 'unknown'}
{frames_text}
{cause_text}

════════ WHAT THE AGENT FOUND ════════
{hyp_text}

Tools used    : {', '.join(set(tools_called)) if tools_called else 'none'}
Failed paths  : {', '.join(failed_searches) if failed_searches else 'none'}

════════ TASK ════════
Produce a JSON object with this exact schema:
{{
  "suspected_class":   "ClassName or UNKNOWN",
  "suspected_method":  "methodName or UNKNOWN",
  "file_path":         "absolute path or UNKNOWN",
  "line_number":       null,
  "call_chain":        [],
  "confidence":        0.0,
  "confidence_reason": "explain what was found and what remains uncertain",
  "technical_summary": "what the agent found, what it could not confirm",
  "business_summary":  "plain English summary for a non-technical person",
  "stop_reason":       "max_iter"
}}

RULES:
- Be honest about uncertainty — do not inflate confidence
- Only reference class names seen in the investigation above
- If frames were provided, they are the strongest signal available
- If no hypothesis formed, report UNKNOWN with explanation

Return ONLY valid JSON. No markdown. No explanation.
"""
