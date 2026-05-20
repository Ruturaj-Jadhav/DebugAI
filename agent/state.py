"""
agent/state.py

Single source of truth for the investigator agent.
Every node reads from AgentState and returns an updated copy.

Key design decisions:
  - Hypothesis is first-class — not buried in messages
  - Strategy controls explore vs exploit mode
  - messages is working memory — LLM reads it whole each turn
  - context_chars_used is lightweight token budget (1 token ≈ 4 chars)
  - failed_searches serves double duty: prevents repetition + feeds smart fallback
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from classifier.schema import ClassifierOutput
    from indexer.index_schema import RepoIndex


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class Strategy:
    EXPLORE  = "explore"
    # Broad scan — search new keywords, find new candidate classes
    # Used when: hypothesis is weak, evidence is thin, agent is stuck

    EXPLOIT  = "exploit"
    # Deep dive — read methods, trace dependencies of one suspect
    # Used when: strong candidate found, hypothesis confidence >= 0.5


# ---------------------------------------------------------------------------
# StopReason
# ---------------------------------------------------------------------------

class StopReason:
    CONFIDENT     = "confident"      # LLM confirmed hypothesis with evidence
    MAX_ITER      = "max_iter"       # hit iteration limit
    CONTEXT_LIMIT = "context_limit"  # context chars exceeded budget
    DEAD_END      = "dead_end"       # nothing found after exhausting searches
    NEEDS_INPUT   = "needs_input"    # input too vague to investigate


# ---------------------------------------------------------------------------
# Hypothesis — first-class citizen, updated after every tool call
# ---------------------------------------------------------------------------

@dataclass
class Hypothesis:
    """
    The agent's current best theory about where the bug is.

    Updated after every tool call — not just at the end.
    INVESTIGATE reads this to decide what to do next:
      1. What is my current hypothesis?
      2. What evidence supports it?
      3. What is still missing?
      4. Which tool addresses the gap?

    This transforms the agent from a random tool-caller
    into a guided investigator.
    """
    suspected_class:  Optional[str]     # "TradeService"
    suspected_method: Optional[str]     # "save"
    reasoning:        str               # why we think this is the bug location
    confidence:       float             # 0.0 – 1.0
    evidence:         list[str]         # facts supporting this hypothesis
    refuted_classes:  list[str]         # classes we ruled out with evidence
    missing:          Optional[str]     # what we still need to confirm

    def is_strong(self) -> bool:
        """Hypothesis is strong enough to move to exploit mode."""
        return (
            self.confidence >= 0.5
            and self.suspected_class is not None
            and len(self.evidence) >= 1
        )

    def is_confident(self) -> bool:
        """Hypothesis is confident enough to stop investigating."""
        return (
            self.confidence >= 0.75
            and self.suspected_class is not None
            and self.suspected_method is not None
            and len(self.evidence) >= 2
        )

    def summary(self) -> str:
        lines = [
            f"Hypothesis: {self.suspected_class or '?'}.{self.suspected_method or '?'}()",
            f"Confidence: {self.confidence:.0%}",
            f"Reasoning : {self.reasoning}",
        ]
        if self.evidence:
            lines.append(f"Evidence  : {' | '.join(self.evidence)}")
        if self.refuted_classes:
            lines.append(f"Ruled out : {', '.join(self.refuted_classes)}")
        if self.missing:
            lines.append(f"Missing   : {self.missing}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# InvestigationResult — produced by OUTPUT node
# ---------------------------------------------------------------------------

@dataclass
class InvestigationResult:
    """
    Final output. Always produced — even on failure paths (with low confidence).
    stop_reason tells the user why we stopped and how reliable the result is.
    """
    # Location
    suspected_class:   str
    suspected_method:  str
    file_path:         str
    line_number:       Optional[int]

    # Call chain — ordered entry point → failure point
    call_chain:        list[str]

    # Confidence
    confidence:        float
    confidence_reason: str

    # Explanation — two audiences
    technical_summary: str             # for developers
    business_summary:  str             # plain English for PO/BA

    # Audit trail
    stop_reason:       str
    tools_used:        list[str]
    iterations:        int
    used_raw_fallback: bool


# ---------------------------------------------------------------------------
# AgentState — flows through every LangGraph node
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """
    Mutable state that flows through the LangGraph.
    Every node receives the full state and returns updated fields.

    Field groups:
      Inputs       → set once at INTAKE, never modified
      Seed         → set by INTAKE, read by INVESTIGATE
      Hypothesis   → updated by INVESTIGATE after every tool call
      Strategy     → set by ROUTE, read by INVESTIGATE
      Memory       → messages grows every iteration
      Control      → iteration counter, limits, stop signal
      Tracking     → failed searches for fallback + dedup
      Output       → set by OUTPUT node
    """

    # ── Inputs ────────────────────────────────────────────────────────
    classifier_output: "ClassifierOutput"
    repo_index:        "RepoIndex"

    # ── Investigation seed (set by INTAKE) ────────────────────────────
    investigation_seed: str
    # Structured starting context. Derived from classifier if succeeded,
    # from raw_input if classifier failed. First message the LLM reads.

    # ── Hypothesis (updated by INVESTIGATE) ───────────────────────────
    hypothesis: Optional[Hypothesis] = None
    # Starts as None, created after first tool call.
    # Drives all subsequent tool selection decisions.

    # ── Strategy (set by ROUTE, read by INVESTIGATE) ──────────────────
    strategy: str = Strategy.EXPLORE
    # EXPLORE → search broadly, find candidates
    # EXPLOIT → deep dive into hypothesis.suspected_class

    # ── Working memory ─────────────────────────────────────────────────
    messages: list[dict] = field(default_factory=list)
    # Full conversation. LLM reads this whole each turn.
    # {"role": "system"|"assistant"|"tool", "content": "..."}

    # ── Control ────────────────────────────────────────────────────────
    iteration:          int = 0
    max_iterations:     int = 5        # 5 developer / 8 business (set by INTAKE)

    context_chars_used: int = 0
    max_context_chars:  int = 24_000   # ~6k tokens, safe for free tier models

    stop_reason:        Optional[str] = None

    # ── Tracking ───────────────────────────────────────────────────────
    failed_searches: list[str] = field(default_factory=list)
    # Keywords or class names that returned empty results.
    # Double duty:
    #   1. INVESTIGATE reads this to avoid repeating dead ends
    #   2. Smart fallback receives this as context for reasoned output

    consecutive_empty: int = 0
    # How many consecutive tool calls returned no useful result.
    # ROUTE uses this to force strategy switch when >= 2.

    # ── Inter-node communication ──────────────────────────────────────────
    _pending_tool:    Optional[str]  = None
    _pending_args:    Optional[dict] = None
    _fallback_result: Optional[dict] = None

    # ── Output (set by OUTPUT node) ────────────────────────────────────
    result: Optional[InvestigationResult] = None

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def add_message(self, role: str, content: str) -> None:
        """Append message and update context budget."""
        self.messages.append({"role": role, "content": content})
        self.context_chars_used += len(content)

    def is_over_budget(self) -> bool:
        return self.context_chars_used > self.max_context_chars

    def should_stop(self) -> bool:
        return (
            self.stop_reason is not None
            or self.iteration >= self.max_iterations
            or self.is_over_budget()
        )

    def tools_called(self) -> list[str]:
        """Extract tool call names from message history."""
        tools = []
        for m in self.messages:
            content = m.get("content", "")
            # Tool results use format: "[Tool result: tool_name]\nresult..."
            if content.startswith("[Tool result:"):
                name = content.split("[Tool result:")[1].split("]")[0].strip()
                tools.append(name)
        return tools

    def effective_stop_reason(self) -> str:
        """Resolve the actual stop reason including implicit ones."""
        if self.stop_reason:
            return self.stop_reason
        if self.is_over_budget():
            return StopReason.CONTEXT_LIMIT
        if self.iteration >= self.max_iterations:
            return StopReason.MAX_ITER
        return StopReason.MAX_ITER

    def status_line(self) -> str:
        """One-line status for logging."""
        hyp = (
            f"{self.hypothesis.suspected_class}.{self.hypothesis.suspected_method}() "
            f"@ {self.hypothesis.confidence:.0%}"
            if self.hypothesis and self.hypothesis.suspected_class
            else "no hypothesis yet"
        )
        return (
            f"iter={self.iteration}/{self.max_iterations} "
            f"strategy={self.strategy} "
            f"ctx={self.context_chars_used}/{self.max_context_chars} "
            f"hypothesis={hyp}"
        )
