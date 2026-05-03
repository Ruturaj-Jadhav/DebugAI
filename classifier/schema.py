"""
classifier/schema.py

Dataclasses for ClassifierOutput.

Changes in this version:
  - Added classifier_succeeded: bool — False means LLM failed, use raw_input
  - Added failure_reason: str | None — why the classifier failed
  - validate() surfaces classifier_succeeded=False as first warning
  - summary() shows extraction status clearly
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Frame:
    class_name:   str
    method_name:  str
    file_name:    str
    line_number:  int
    is_user_code: bool
    repeated:     bool = False

    @staticmethod
    def from_dict(d: dict) -> "Frame":
        return Frame(
            class_name   = d.get("class_name", ""),
            method_name  = d.get("method_name", ""),
            file_name    = d.get("file_name", ""),
            line_number  = int(d.get("line_number", 0)),
            is_user_code = bool(d.get("is_user_code", False)),
            repeated     = bool(d.get("repeated", False)),
        )

    def display(self) -> str:
        return f"{self.class_name}.{self.method_name}({self.file_name}:{self.line_number})"


@dataclass
class LogLine:
    raw:        str
    level:      Optional[str]
    class_name: Optional[str]

    @staticmethod
    def from_dict(d: dict) -> "LogLine":
        if isinstance(d, str):
            return LogLine(raw=d, level=None, class_name=None)
        return LogLine(
            raw        = d.get("raw", str(d)),
            level      = d.get("level"),
            class_name = d.get("class_name"),
        )


@dataclass
class Intent:
    action:     Optional[str]
    feature:    Optional[str]
    endpoint:   Optional[str]
    entity_ids: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "Intent":
        return Intent(
            action     = d.get("action"),
            feature    = d.get("feature"),
            endpoint   = d.get("endpoint"),
            entity_ids = d.get("entity_ids") or [],
        )


@dataclass
class Failure:
    error_type:    Optional[str]
    error_message: Optional[str]
    http_status:   Optional[int]
    symptom:       str

    @staticmethod
    def from_dict(d: dict) -> "Failure":
        status = d.get("http_status")
        return Failure(
            error_type    = d.get("error_type"),
            error_message = d.get("error_message"),
            http_status   = int(status) if status else None,
            symptom       = d.get("symptom", ""),
        )


@dataclass
class Location:
    primary_frame: Optional[Frame]

    @staticmethod
    def from_dict(d: dict) -> "Location":
        pf = d.get("primary_frame")
        return Location(primary_frame=Frame.from_dict(pf) if pf else None)


@dataclass
class Cause:
    error_type:    Optional[str]
    error_message: Optional[str]

    @staticmethod
    def from_dict(d: dict) -> "Cause":
        return Cause(
            error_type    = d.get("error_type"),
            error_message = d.get("error_message"),
        )


@dataclass
class Evidence:
    frames:            list[Frame]
    logs:              list[LogLine]
    raw_stack_present: bool
    raw_log_present:   bool

    @staticmethod
    def from_dict(d: dict) -> "Evidence":
        return Evidence(
            frames            = [Frame.from_dict(f) for f in d.get("frames", [])],
            logs              = [LogLine.from_dict(l) for l in d.get("logs", [])],
            raw_stack_present = bool(d.get("raw_stack_present", False)),
            raw_log_present   = bool(d.get("raw_log_present", False)),
        )

    def user_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.is_user_code]


# ---------------------------------------------------------------------------
# Failure reasons — used when classifier_succeeded = False
# ---------------------------------------------------------------------------

class FailureReason:
    API_TIMEOUT     = "api_timeout"
    INVALID_JSON    = "invalid_json"
    EMPTY_RESPONSE  = "empty_response"
    API_ERROR       = "api_error"
    PARSE_ERROR     = "parse_error"


# ---------------------------------------------------------------------------
# Top level output
# ---------------------------------------------------------------------------

@dataclass
class ClassifierOutput:
    mode:               str
    intent:             Intent
    failure:            Failure
    location:           Location
    cause:              Optional[Cause]
    evidence:           Evidence
    additional_context: Optional[str]
    confidence:         float
    raw_input:          str

    # --- New fields ---
    classifier_succeeded: bool          = True
    # True  → LLM call succeeded, all fields populated from extraction
    # False → LLM call failed, all fields are empty, use raw_input directly

    failure_reason: Optional[str]       = None
    # Populated only when classifier_succeeded = False
    # Values: "api_timeout" | "invalid_json" | "empty_response" | "api_error" | "parse_error"

    # ------------------------------------------------------------------
    # Deserialiser
    # ------------------------------------------------------------------

    @staticmethod
    def from_dict(d: dict, raw_input: str) -> "ClassifierOutput":
        cause_raw = d.get("cause")
        evidence  = Evidence.from_dict(d.get("evidence") or {})

        # Compute primary_frame deterministically — never trust LLM for this.
        # Rule: first is_user_code=True frame from evidence.frames.
        # LLM-returned location.primary_frame is intentionally ignored.
        primary_frame = next(
            (f for f in evidence.frames if f.is_user_code),
            None
        )

        return ClassifierOutput(
            mode                 = d.get("mode", "developer"),
            intent               = Intent.from_dict(d.get("intent") or {}),
            failure              = Failure.from_dict(d.get("failure") or {}),
            location             = Location(primary_frame=primary_frame),
            cause                = Cause.from_dict(cause_raw) if cause_raw else None,
            evidence             = evidence,
            additional_context   = d.get("additional_context"),
            confidence           = float(d.get("confidence", 0.0)),
            raw_input            = raw_input,
            classifier_succeeded = True,
            failure_reason       = None,
        )

    @staticmethod
    def make_failed(
        raw_input:      str,
        mode:           Optional[str],
        failure_reason: str,
    ) -> "ClassifierOutput":
        """
        Build a failed ClassifierOutput.

        classifier_succeeded = False signals the investigator agent to
        skip all extracted fields and work directly from raw_input.

        This is intentionally NOT a deterministic fallback with fake values —
        fake values would mislead the agent. Empty + flag is honest.
        """
        return ClassifierOutput(
            mode                 = mode or "developer",
            intent               = Intent(action=None, feature=None, endpoint=None, entity_ids=[]),
            failure              = Failure(error_type=None, error_message=None, http_status=None, symptom=""),
            location             = Location(primary_frame=None),
            cause                = None,
            evidence             = Evidence(frames=[], logs=[], raw_stack_present=False, raw_log_present=False),
            additional_context   = None,
            confidence           = 0.0,
            raw_input            = raw_input,
            classifier_succeeded = False,
            failure_reason       = failure_reason,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """
        Returns list of warning strings. Never raises.
        classifier_succeeded=False is always the first warning if present —
        the investigator agent checks this before reading any other field.
        """
        warnings = []

        # This must be first — agent reads warnings in order
        if not self.classifier_succeeded:
            warnings.append(
                f"classifier_failed:{self.failure_reason} — "
                f"investigator agent must use raw_input directly"
            )
            return warnings  # no point checking other fields — all empty

        if not self.failure.symptom:
            warnings.append("symptom is empty — LLM may have failed to extract")

        if self.mode == "developer" and not self.evidence.raw_stack_present and not self.evidence.raw_log_present:
            warnings.append("developer mode but no stack trace or logs detected")

        if self.confidence < 0.3:
            warnings.append(
                f"low confidence ({self.confidence}) — input may be too vague to investigate"
            )

        if self.evidence.frames and not self.evidence.user_frames():
            warnings.append(
                "frames found but none marked as user code — check package prefix"
            )

        return warnings

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def summary(self) -> str:
        if not self.classifier_succeeded:
            return (
                f"Classifier  : FAILED ({self.failure_reason})\n"
                f"Agent will  : use raw_input directly\n"
                f"Raw input   : {self.raw_input[:120]}{'...' if len(self.raw_input) > 120 else ''}"
            )

        lines = [
            f"Classifier  : OK",
            f"Mode        : {self.mode}",
            f"Confidence  : {self.confidence}",
            f"Symptom     : {self.failure.symptom}",
        ]
        if self.failure.error_type:
            lines.append(f"Error       : {self.failure.error_type}")
        if self.failure.http_status:
            lines.append(f"HTTP        : {self.failure.http_status}")
        if self.intent.endpoint:
            lines.append(f"Endpoint    : {self.intent.endpoint}")
        if self.intent.action:
            lines.append(f"Action      : {self.intent.action}")
        if self.intent.feature:
            lines.append(f"Feature     : {self.intent.feature}")
        if self.intent.entity_ids:
            lines.append(f"Entity IDs  : {self.intent.entity_ids}")
        if self.location.primary_frame:
            lines.append(f"Primary     : {self.location.primary_frame.display()}")
        if self.cause:
            lines.append(f"Root cause  : {self.cause.error_type} — {self.cause.error_message}")
        if self.evidence.user_frames():
            lines.append("User frames :")
            for f in self.evidence.user_frames():
                lines.append(f"  → {f.display()}")
        if self.additional_context:
            lines.append(f"Context     : {self.additional_context}")
        return "\n".join(lines)
