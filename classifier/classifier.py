"""
classifier/classifier.py

Makes a single Groq LLM call and returns ClassifierOutput.

Changes in this version:
  - Each failure path sets a specific failure_reason
  - Uses ClassifierOutput.make_failed() instead of silent empty fallback
  - Groq-specific exceptions caught separately for precise reason codes
"""

import json
import os
from groq import Groq
from groq import APITimeoutError, APIConnectionError, APIStatusError

from prompt import build_prompt
from schema import ClassifierOutput, FailureReason

_MODEL       = "llama-3.3-70b-versatile"
_TEMPERATURE = 0
_MAX_TOKENS  = 2000


class Classifier:

    def __init__(self, package_prefix: str | None = None, api_key: str | None = None):
        self.package_prefix = package_prefix
        self.client         = Groq(api_key='' or os.environ.get("GROQ_API_KEY"))
        self.system_prompt  = build_prompt(package_prefix)

    def classify(self, user_input: str, mode: str | None = None) -> ClassifierOutput:
        """
        Extract structured debugging information from raw user input.
        Always returns a ClassifierOutput — never raises.

        When LLM fails:
          classifier_succeeded = False
          failure_reason       = specific reason code
          raw_input            = original input preserved
          → investigator agent reads raw_input directly
        """
        prefixed_input = self._prefix_input(user_input, mode)
        output         = self._extract(user_input, mode, prefixed_input)

        # Validate and surface warnings
        warnings = output.validate()
        for w in warnings:
            print(f"[classifier] WARN: {w}")

        return output

    # ------------------------------------------------------------------
    # Extraction with specific failure handling
    # ------------------------------------------------------------------

    def _extract(
        self,
        user_input:     str,
        mode:           str | None,
        prefixed_input: str,
    ) -> ClassifierOutput:

        # ── LLM call ────────────────────────────────────────────────────
        try:
            raw_json = self._call_llm(prefixed_input)
        except APITimeoutError:
            print("[classifier] LLM call timed out")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_TIMEOUT)
        except APIConnectionError:
            print("[classifier] LLM connection failed")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_ERROR)
        except APIStatusError as e:
            print(f"[classifier] LLM API error: {e.status_code}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_ERROR)
        except Exception as e:
            print(f"[classifier] Unexpected LLM error: {e}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_ERROR)

        # ── Empty response check ─────────────────────────────────────────
        if not raw_json or not raw_json.strip():
            print("[classifier] LLM returned empty response")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.EMPTY_RESPONSE)

        # ── JSON parse ───────────────────────────────────────────────────
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as e:
            print(f"[classifier] LLM returned invalid JSON: {e}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.INVALID_JSON)

        # ── Schema deserialisation ───────────────────────────────────────
        try:
            output = ClassifierOutput.from_dict(parsed, raw_input=user_input)
        except Exception as e:
            print(f"[classifier] Failed to parse LLM response into schema: {e}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.PARSE_ERROR)

        return output

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _prefix_input(self, user_input: str, mode: str | None) -> str:
        if mode == "business":
            return f"[BUSINESS]\n{user_input}"
        if mode == "developer":
            return f"[DEVELOPER]\n{user_input}"
        return user_input

    def _call_llm(self, prefixed_input: str) -> str:
        response = self.client.chat.completions.create(
            model           = _MODEL,
            temperature     = _TEMPERATURE,
            max_tokens      = _MAX_TOKENS,
            messages        = [
                { "role": "system", "content": self.system_prompt },
                { "role": "user",   "content": prefixed_input      },
            ],
            response_format = { "type": "json_object" },
        )
        return response.choices[0].message.content
