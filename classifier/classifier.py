"""
classifier/classifier.py

Makes a single LLM call and returns ClassifierOutput.

Response is streamed and collected into a single string before parsing.

Note: This model does not support response_format: json_object.
     We rely on the prompt's strict JSON instructions instead.
     A JSON extraction fallback handles any surrounding text.
"""

import json
import os
import re
from openai import OpenAI, APITimeoutError, APIConnectionError, APIStatusError

from .prompt import build_prompt
from .schema import ClassifierOutput, FailureReason

_BASE_URL    = "https://openrouter.ai/api/v1"
_MODEL       = "openai/gpt-oss-120b:free"
_TEMPERATURE = 0        # deterministic — extraction not generation
_MAX_TOKENS  = 1500     # enough for ClassifierOutput JSON, not more


class Classifier:

    def __init__(self, package_prefix: str | None = None, api_key: str | None = None):
        """
        package_prefix: e.g. "com.example" — marks user code frames.
                        Falls back to blocklist-based detection if None.
        """
        self.package_prefix = package_prefix
        self.client = OpenAI(
            base_url = _BASE_URL,
             api_key  = api_key or os.environ.get("OPENROUTER_API_KEY", ""),
        )
        self.system_prompt = build_prompt(package_prefix)

    def classify(self, user_input: str, mode: str | None = None) -> ClassifierOutput:
        """
        Extract structured debugging info from raw user input.
        Always returns ClassifierOutput — never raises.

        On LLM failure:
          classifier_succeeded = False
          failure_reason       = specific reason code
          raw_input            = preserved for investigator agent
        """
        prefixed_input = self._prefix_input(user_input, mode)
        output         = self._extract(user_input, mode, prefixed_input)

        warnings = output.validate()
        for w in warnings:
            print(f"[classifier] WARN: {w}")

        return output

    # ------------------------------------------------------------------
    # Extraction pipeline
    # ------------------------------------------------------------------

    def _extract(self, user_input: str, mode: str | None, prefixed_input: str) -> ClassifierOutput:

        # ── LLM call (streaming) ─────────────────────────────────────────
        try:
            raw_text = self._call_llm(prefixed_input)
        except APITimeoutError:
            print("[classifier] LLM call timed out")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_TIMEOUT)
        except APIConnectionError:
            print("[classifier] LLM connection failed")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_ERROR)
        except APIStatusError as e:
            print(f"[classifier] LLM API error: {e.status_code} — {e.message}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_ERROR)
        except Exception as e:
            print(f"[classifier] Unexpected error: {e}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.API_ERROR)

        # ── Empty response ───────────────────────────────────────────────
        if not raw_text or not raw_text.strip():
            print("[classifier] LLM returned empty response")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.EMPTY_RESPONSE)

        # ── JSON extraction ──────────────────────────────────────────────
        # Model may wrap JSON in markdown fences or add preamble text.
        # We extract the first valid JSON object from the response.
        raw_json = self._extract_json(raw_text)
        if raw_json is None:
            print(f"[classifier] Could not find JSON in response:\n{raw_text[:300]}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.INVALID_JSON)

        # ── Parse JSON ───────────────────────────────────────────────────
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as e:
            print(f"[classifier] JSON parse error: {e}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.INVALID_JSON)

        # ── Build schema object ──────────────────────────────────────────
        try:
            output = ClassifierOutput.from_dict(parsed, raw_input=user_input)
        except Exception as e:
            print(f"[classifier] Schema parse error: {e}")
            return ClassifierOutput.make_failed(user_input, mode, FailureReason.PARSE_ERROR)

        return output

    # ------------------------------------------------------------------
    # LLM call — streaming, collected into single string
    # ------------------------------------------------------------------

    def _call_llm(self, prefixed_input: str) -> str:
        """
        Stream response from NVIDIA NIM and collect into a single string.
        Streaming is required by this endpoint — we collect all chunks.
        """
        completion = self.client.chat.completions.create(
            model       = _MODEL,
            temperature = _TEMPERATURE,
            max_tokens  = _MAX_TOKENS,
            stream      = True,
            messages    = [
                { "role": "system", "content": self.system_prompt },
                { "role": "user",   "content": prefixed_input      },
            ],
        )

        chunks = []
        for chunk in completion:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                chunks.append(delta.content)

        return "".join(chunks)

    # ------------------------------------------------------------------
    # JSON extraction — handles markdown fences and preamble text
    # ------------------------------------------------------------------

    def _extract_json(self, text: str) -> str | None:
        """
        Extract the first valid JSON object from the LLM response.

        Handles three common formats the model might return:
          1. Pure JSON                    → {"mode": ...}
          2. Markdown fenced              → ```json\n{...}\n```
          3. Preamble then JSON           → "Here is the output:\n{...}"
        """
        text = text.strip()

        # Format 1: entire response is already valid JSON
        if text.startswith("{"):
            return text

        # Format 2: markdown code fence
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            return fence_match.group(1)

        # Format 3: find first { ... } block in the text
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            return brace_match.group(0)

        return None

    # ------------------------------------------------------------------
    # Mode prefix
    # ------------------------------------------------------------------

    def _prefix_input(self, user_input: str, mode: str | None) -> str:
        if mode == "business":
            return f"[BUSINESS]\n{user_input}"
        if mode == "developer":
            return f"[DEVELOPER]\n{user_input}"
        return user_input
