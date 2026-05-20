"""
agent/tools.py

Three deterministic tools the investigator agent can call.
No LLM — pure index queries and file reads.

Each returns a plain string appended to messages.
The LLM reads these strings as evidence for its next reasoning step.

Design:
  search_index       → broad, used in EXPLORE mode
  get_class_summary  → medium depth, bridges explore→exploit
  read_method        → deepest, used in EXPLOIT mode, 80 line cap
"""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from indexer.index_schema import RepoIndex

_MAX_METHOD_LINES = 80
_MAX_SEARCH_RESULTS = 5
_MAX_METHODS_IN_SUMMARY = 10


# ---------------------------------------------------------------------------
# Tool 1 — search_index  (EXPLORE mode)
# ---------------------------------------------------------------------------

def search_index(keyword: str, repo_index: "RepoIndex") -> str:
    """
    Keyword search across class names, method names, endpoint URLs.
    Returns structural overview — no source code.
    Used when: starting broad, recovering from wrong direction.
    """
    if not keyword or not keyword.strip():
        return "search_index: empty keyword provided — please provide a search term"

    results = repo_index.search_by_keyword(keyword.strip(), prod_only=True)

    if not results:
        return (
            f"search_index('{keyword}'): no classes found. "
            f"Try a different keyword or check if the repo is indexed."
        )

    lines = [f"search_index('{keyword}'): {len(results)} matches"]

    for cls in results[:_MAX_SEARCH_RESULTS]:
        lines.append(f"\n  [{cls.class_type.value}] {cls.class_name}")
        lines.append(f"    file: {cls.file_path}")

        if cls.endpoints:
            for ep in cls.endpoints[:3]:
                path = ep.full_paths[0] if ep.full_paths else ""
                lines.append(
                    f"    endpoint: [{ep.http_method.value}] {path}"
                    f" → {ep.handler_name}() line {ep.line_number}"
                )

        if cls.dependencies:
            dep_names = [d.class_name for d in cls.dependencies[:4]]
            lines.append(f"    depends on: {', '.join(dep_names)}")

    if len(results) > _MAX_SEARCH_RESULTS:
        lines.append(f"\n  ... {len(results) - _MAX_SEARCH_RESULTS} more (refine keyword)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2 — get_class_summary  (EXPLORE → EXPLOIT bridge)
# ---------------------------------------------------------------------------

def get_class_summary(class_name: str, repo_index: "RepoIndex") -> str:
    """
    Structural summary of one class — methods, endpoints, dependencies.
    No source code. Helps agent decide which method to read next.
    Used when: agent has a candidate class, needs to narrow to method.
    """
    if not class_name or not class_name.strip():
        return "get_class_summary: no class name provided"

    cls = repo_index.find_by_class_name(class_name.strip())

    if cls is None:
        return (
            f"get_class_summary('{class_name}'): not found in index. "
            f"This may be an external library class not in this repo."
        )

    lines = [
        f"get_class_summary('{cls.class_name}') [{cls.class_type.value}]",
        f"  package : {cls.package}",
        f"  file    : {cls.file_path}",
    ]

    if cls.endpoints:
        lines.append(f"  endpoints ({len(cls.endpoints)}):")
        for ep in cls.endpoints:
            path = ep.full_paths[0] if ep.full_paths else ""
            lines.append(
                f"    [{ep.http_method.value}] {path}"
                f" → {ep.handler_method_id} line {ep.line_number}"
            )

    if cls.methods:
        lines.append(f"  methods ({len(cls.methods)}):")
        for m in cls.methods[:_MAX_METHODS_IN_SUMMARY]:
            lines.append(f"    {m.method_id} line {m.line_number}")
        if len(cls.methods) > _MAX_METHODS_IN_SUMMARY:
            lines.append(f"    ... {len(cls.methods) - _MAX_METHODS_IN_SUMMARY} more")

    if cls.dependencies:
        lines.append(f"  dependencies:")
        for d in cls.dependencies:
            lines.append(
                f"    {d.field_name}: {d.class_name} [{d.dependency_kind}]"
            )

    dependents = repo_index.find_dependents_of(class_name.strip())
    if dependents:
        callers = list({e.caller_class for e in dependents})[:4]
        lines.append(f"  called by: {', '.join(callers)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3 — read_method  (EXPLOIT mode)
# ---------------------------------------------------------------------------

def read_method(
    class_name:  str,
    method_name: str,
    repo_index:  "RepoIndex",
) -> str:
    """
    Read actual Java source of one method. Capped at 80 lines.
    The deepest tool — only called when agent has a strong suspect.
    If method > 80 lines: returns first 40 + last 40 with a gap marker.
    """
    if not class_name or not method_name:
        return "read_method: class_name and method_name are required"

    cls = repo_index.find_by_class_name(class_name.strip())
    if cls is None:
        return (
            f"read_method('{class_name}', '{method_name}'): "
            f"class not found in index"
        )

    # Find method — check both regular methods and endpoint handlers
    target_line = None
    target_name = None

    for m in cls.methods:
        if m.name.lower() == method_name.lower().split("(")[0]:
            target_line = m.line_number
            target_name = m.name
            break

    if target_line is None:
        for ep in cls.endpoints:
            if ep.handler_name.lower() == method_name.lower().split("(")[0]:
                target_line = ep.line_number
                target_name = ep.handler_name
                break

    if target_line is None:
        available = (
            [m.name for m in cls.methods]
            + [ep.handler_name for ep in cls.endpoints]
        )
        return (
            f"read_method('{class_name}', '{method_name}'): method not found. "
            f"Available: {', '.join(available[:10])}"
        )

    # Read source file
    try:
        source_lines = Path(cls.file_path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError) as e:
        return f"read_method: could not read '{cls.file_path}': {e}"

    start = max(0, target_line - 1)
    end   = min(len(source_lines), start + _MAX_METHOD_LINES)
    body  = source_lines[start:end]

    result_lines = [
        f"read_method('{class_name}', '{target_name}') "
        f"starting line {target_line}",
    ]

    # If method is longer than cap: first 40 + separator + last 40
    if len(source_lines) - start > _MAX_METHOD_LINES:
        result_lines.append(
            f"[method exceeds {_MAX_METHOD_LINES} lines — "
            f"showing first 40 and last 40]"
        )
        result_lines.append("```java")
        result_lines.extend(body[:40])
        result_lines.append(f"    // ... middle section omitted ...")
        result_lines.extend(body[-40:])
        result_lines.append("```")
    else:
        result_lines.append("```java")
        result_lines.extend(body)
        result_lines.append("```")

    return "\n".join(result_lines)


# ---------------------------------------------------------------------------
# Tool dispatcher — called by TOOL CALL node
# ---------------------------------------------------------------------------

AVAILABLE_TOOLS = {
    "search_index": {
        "description": "Search repo by keyword. Returns matching classes with structure.",
        "args": {"keyword": "string to search for"},
    },
    "get_class_summary": {
        "description": "Get structural summary of one class — methods, endpoints, dependencies. No source code.",
        "args": {"class_name": "exact class name to summarise"},
    },
    "read_method": {
        "description": "Read actual Java source of one method. Max 80 lines.",
        "args": {
            "class_name":  "class containing the method",
            "method_name": "method to read",
        },
    },
}


def dispatch_tool(
    tool_name:  str,
    tool_args:  dict,
    repo_index: "RepoIndex",
) -> str:
    """
    Execute a tool by name. Returns string result.
    Called by TOOL CALL node — no LLM involved.
    """
    if tool_name == "search_index":
        return search_index(tool_args.get("keyword", ""), repo_index)

    elif tool_name == "get_class_summary":
        return get_class_summary(tool_args.get("class_name", ""), repo_index)

    elif tool_name == "read_method":
        return read_method(
            tool_args.get("class_name", ""),
            tool_args.get("method_name", ""),
            repo_index,
        )

    else:
        available = ", ".join(AVAILABLE_TOOLS.keys())
        return f"unknown tool '{tool_name}'. Available: {available}"


def is_empty_result(tool_result: str) -> bool:
    """
    Detect whether a tool call returned no useful information.
    Used by TOOL CALL node to update consecutive_empty counter.
    """
    empty_signals = [
        "no classes found",
        "not found in index",
        "method not found",
        "could not read",
        "empty keyword",
        "unknown tool",
    ]
    result_lower = tool_result.lower()
    return any(signal in result_lower for signal in empty_signals)