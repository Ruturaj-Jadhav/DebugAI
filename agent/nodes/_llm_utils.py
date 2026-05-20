"""
agent/nodes/_llm_utils.py

Shared LLM utility used by all nodes.
Centralises: client creation, streaming, retry on 429, JSON extraction.
"""

import os
import re
import json
import time
from openai import OpenAI, RateLimitError

_BASE_URL   = "https://openrouter.ai/api/v1"
_MODEL      = "openai/gpt-oss-120b:free"
_MAX_TOKENS = 1500
_TEMP       = 0


def call_llm(messages: list[dict], max_retries: int = 1) -> str:
    """
    Call the LLM with automatic retry on 429 rate limit.
    Uses retry_after_seconds from the error response when available.
    """
    client = OpenAI(
        base_url = _BASE_URL,
        api_key  = os.environ.get("OPENROUTER_API_KEY", ""),
    )

    for attempt in range(max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model       = _MODEL,
                temperature = _TEMP,
                max_tokens  = _MAX_TOKENS,
                stream      = True,
                messages    = messages,
            )
            chunks = []
            for chunk in completion:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    chunks.append(delta.content)
            return "".join(chunks)

        except RateLimitError as e:
            if attempt < max_retries:
                wait = 30
                try:
                    meta = e.body.get("error", {}).get("metadata", {})
                    wait = int(meta.get("retry_after_seconds", 30)) + 2
                except Exception:
                    pass
                print(f"[llm] Rate limited — waiting {wait}s then retrying")
                time.sleep(wait)
            else:
                raise


def extract_json(raw: str) -> dict | None:
    """
    Extract the first valid JSON object from LLM response.
    Uses raw_decode to stop at first complete object — ignores trailing content.
    Handles markdown fences and preamble text.
    """
    text = raw.strip()

    # Find start of first JSON object
    json_start = None
    fence = re.search(r"```(?:json)?\s*(\{)", text, re.DOTALL)
    if fence:
        json_start = fence.start(1)
    elif text.startswith("{"):
        json_start = 0
    else:
        brace = re.search(r"\{", text)
        if brace:
            json_start = brace.start()

    if json_start is None:
        return None

    try:
        decoder = json.JSONDecoder()
        result, _ = decoder.raw_decode(text, json_start)
        return result
    except json.JSONDecodeError:
        return None
