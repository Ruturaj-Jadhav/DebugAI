"""
cli.py — DebugAI entry point

Usage:
    python cli.py --repo ./my-java-project --error error.txt
    python cli.py --repo ./my-java-project                     (paste inline)
    python cli.py --repo ./my-java-project --error error.txt --reindex
    python cli.py --repo ./my-java-project --error error.txt --json
    python cli.py --repo ./my-java-project --error error.txt --package com.example

Environment:
    OPENROUTER_API_KEY   required
    DEBUGAI_MODEL        optional (default: meta-llama/llama-3.3-70b-instruct:free)
    DEBUGAI_PACKAGE      optional package prefix for frame filtering
"""

import os
import sys
import json
import argparse
import textwrap
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# Colour helpers — degrade gracefully on Windows without ANSI support
# ---------------------------------------------------------------------------

def _supports_colour() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

BOLD   = "\033[1m"   if _supports_colour() else ""
GREEN  = "\033[92m"  if _supports_colour() else ""
YELLOW = "\033[93m"  if _supports_colour() else ""
RED    = "\033[91m"  if _supports_colour() else ""
CYAN   = "\033[96m"  if _supports_colour() else ""
DIM    = "\033[2m"   if _supports_colour() else ""
RESET  = "\033[0m"   if _supports_colour() else ""


def _bar(char="─", width=60) -> str:
    return char * width


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="debugai",
        description="AI-powered Java debugging assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python cli.py --repo ./spring-petclinic --error error.txt
          python cli.py --repo ./spring-petclinic --error error.txt --package org.springframework.samples
          python cli.py --repo ./spring-petclinic --error error.txt --reindex --json
        """)
    )
    parser.add_argument(
        "--repo", required=True,
        help="Path to the Java repository to debug"
    )
    parser.add_argument(
        "--error", default=None,
        help="Path to error file (stack trace / logs). If omitted, input is read from stdin."
    )
    parser.add_argument(
        "--package", default=None,
        help="Your application's base package (e.g. com.example). "
             "Used to filter framework frames from stack traces. "
             "Falls back to DEBUGAI_PACKAGE env var."
    )
    parser.add_argument(
        "--reindex", action="store_true",
        help="Force a fresh index even if a cached one exists"
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output raw JSON instead of human-readable format"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Input reading
# ---------------------------------------------------------------------------

def read_error_input(error_path: str | None) -> str:
    """Read error text from file or stdin."""
    if error_path:
        path = Path(error_path)
        if not path.exists():
            print(f"{RED}Error: file not found: {error_path}{RESET}", file=sys.stderr)
            sys.exit(1)
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            print(f"{RED}Error: error file is empty: {error_path}{RESET}", file=sys.stderr)
            sys.exit(1)
        return text

    # Stdin / interactive paste
    if sys.stdin.isatty():
        print(f"{CYAN}Paste your error (stack trace, logs, or description).{RESET}")
        print(f"{DIM}Press Ctrl+D (Mac/Linux) or Ctrl+Z then Enter (Windows) when done.{RESET}")
        print()
    try:
        text = sys.stdin.read().strip()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Cancelled.{RESET}")
        sys.exit(0)

    if not text:
        print(f"{RED}Error: no input provided.{RESET}", file=sys.stderr)
        sys.exit(1)
    return text


# ---------------------------------------------------------------------------
# Step display helpers
# ---------------------------------------------------------------------------

def _step(n: int, label: str):
    print(f"\n{BOLD}{DIM}[{n}/3]{RESET} {BOLD}{label}{RESET}")


def _ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def _warn(msg: str):
    print(f"  {YELLOW}⚠{RESET}  {msg}")


def _fail(msg: str):
    print(f"  {RED}✗{RESET} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------

def print_result(result, mode: str, iterations: int):
    from agent.state import StopReason

    print(f"\n{_bar('═')}")
    print(f"{BOLD}  DebugAI — Investigation Result{RESET}")
    print(_bar('═'))

    # Confidence colouring
    conf = result.confidence
    conf_colour = GREEN if conf >= 0.7 else YELLOW if conf >= 0.4 else RED
    conf_label  = "High" if conf >= 0.7 else "Medium" if conf >= 0.4 else "Low"

    print(f"\n{BOLD}  Location{RESET}")
    print(f"  {_bar('─', 40)}")

    if result.suspected_class == "UNKNOWN":
        print(f"  {RED}Could not locate the bug in the codebase.{RESET}")
    else:
        print(f"  Class   : {BOLD}{result.suspected_class}{RESET}")
        print(f"  Method  : {BOLD}{result.suspected_method}(){RESET}")
        if result.line_number:
            print(f"  Line    : {result.line_number}")
        if result.file_path and result.file_path != "UNKNOWN":
            # Show relative-looking path
            fp = result.file_path.replace("\\", "/")
            src_idx = fp.find("/src/main/java/")
            display_path = fp[src_idx+1:] if src_idx >= 0 else fp
            print(f"  File    : {DIM}{display_path}{RESET}")

        if result.call_chain:
            print(f"\n  Call chain:")
            for i, step in enumerate(result.call_chain):
                arrow = "  →" if i > 0 else "  ┌"
                print(f"  {arrow} {step}")

    print(f"\n{BOLD}  Confidence{RESET}  {conf_colour}{conf_label} ({conf:.0%}){RESET}")
    if result.confidence_reason:
        print(f"  {DIM}{result.confidence_reason}{RESET}")

    print(f"\n{BOLD}  Technical Summary{RESET}")
    print(f"  {_bar('─', 40)}")
    for line in textwrap.wrap(result.technical_summary, width=70):
        print(f"  {line}")

    print(f"\n{BOLD}  Business Summary{RESET}")
    print(f"  {_bar('─', 40)}")
    for line in textwrap.wrap(result.business_summary, width=70):
        print(f"  {line}")

    print(f"\n{BOLD}  Meta{RESET}")
    print(f"  {_bar('─', 40)}")
    print(f"  Mode       : {mode}")
    print(f"  Iterations : {iterations}")
    print(f"  Tools used : {', '.join(result.tools_used) if result.tools_used else 'none'}")
    print(f"  Stop reason: {result.stop_reason}")
    if result.used_raw_fallback:
        print(f"  {YELLOW}Note: Result produced via raw fallback — lower confidence{RESET}")

    # Guidance for low confidence / failure cases
    if result.stop_reason == StopReason.NEEDS_INPUT:
        print(f"\n  {YELLOW}Tip: Input was too vague. Provide a stack trace or logs for better results.{RESET}")
    elif result.stop_reason == StopReason.DEAD_END:
        print(f"\n  {YELLOW}Tip: No matching classes found. Try --reindex or check --package is correct.{RESET}")

    print(f"\n{_bar('═')}\n")


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def print_json(result, mode: str, classifier_output, elapsed: float):
    output = {
        "status":             "success" if result.suspected_class != "UNKNOWN" else "not_found",
        "mode":               mode,
        "elapsed_seconds":    round(elapsed, 1),
        "suspected_class":    result.suspected_class,
        "suspected_method":   result.suspected_method,
        "file_path":          result.file_path,
        "line_number":        result.line_number,
        "call_chain":         result.call_chain,
        "confidence":         round(result.confidence, 2),
        "confidence_reason":  result.confidence_reason,
        "technical_summary":  result.technical_summary,
        "business_summary":   result.business_summary,
        "stop_reason":        result.stop_reason,
        "tools_used":         result.tools_used,
        "iterations":         result.iterations,
        "used_raw_fallback":  result.used_raw_fallback,
    }
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # ── Check API key ──────────────────────────────────────────────────
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print(f"{RED}Error: OPENROUTER_API_KEY environment variable not set.{RESET}", file=sys.stderr)
        print(f"Get a free key at https://openrouter.ai", file=sys.stderr)
        sys.exit(1)

    # ── Resolve package prefix ─────────────────────────────────────────
    package = args.package or os.environ.get("DEBUGAI_PACKAGE")

    # ── Banner ─────────────────────────────────────────────────────────
    if not args.json_output:
        print(f"\n{BOLD}  DebugAI — Java Debugging Assistant{RESET}")
        print(f"  {DIM}Repo: {args.repo}{RESET}")
        if package:
            print(f"  {DIM}Package: {package}{RESET}")
        print()

    start_time = datetime.now()

    # ── Step 1: Read error input ───────────────────────────────────────
    if not args.json_output:
        _step(1, "Reading error input")

    error_text = read_error_input(args.error)

    if not args.json_output:
        lines = error_text.count("\n") + 1
        _ok(f"{lines} line(s) read")
        preview = error_text[:80].replace("\n", " ")
        print(f"  {DIM}Preview: {preview}...{RESET}")

    # ── Step 2: Index repository ───────────────────────────────────────
    if not args.json_output:
        _step(2, "Indexing repository")

    repo_path = Path(args.repo).resolve()
    if not repo_path.exists():
        _fail(f"Repository not found: {args.repo}")
        sys.exit(1)

    try:
        from indexer.index_builder import IndexBuilder
        builder = IndexBuilder(str(repo_path))

        if args.reindex:
            if not args.json_output:
                print(f"  {DIM}Force reindexing...{RESET}")
            index = builder.build()
        else:
            index = builder.get_or_build()

        prod_count = sum(1 for c in index.classes if c.source_set == "main")
        if not args.json_output:
            _ok(f"{prod_count} production classes indexed, {len(index.dependency_edges)} dependency edges")

    except Exception as e:
        _fail(f"Indexing failed: {e}")
        sys.exit(1)

    # ── Step 3: Classify + Investigate ────────────────────────────────
    if not args.json_output:
        _step(3, "Investigating")
        print(f"  {DIM}Classifying error...{RESET}")

    try:
        from classifier.classifier import Classifier
        classifier = Classifier(package_prefix=package, api_key=api_key)
        co = classifier.classify(error_text)

        if not args.json_output:
            mode_display = co.mode.upper()
            conf_display = f"{co.confidence:.0%}"
            _ok(f"Mode detected: {mode_display} (confidence {conf_display})")
            if not co.classifier_succeeded:
                _warn(f"Classifier failed ({co.failure_reason}) — using raw input")
            if co.failure.symptom:
                print(f"  {DIM}Symptom: {co.failure.symptom[:80]}{RESET}")

    except Exception as e:
        _fail(f"Classification failed: {e}")
        sys.exit(1)

    if not args.json_output:
        print(f"  {DIM}Running investigator agent...{RESET}")

    try:
        from agent.graph import run_investigation
        final = run_investigation(co, index)
        result = final.result

    except Exception as e:
        _fail(f"Investigation failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = (datetime.now() - start_time).total_seconds()

    # ── Output ─────────────────────────────────────────────────────────
    if args.json_output:
        print_json(result, co.mode, co, elapsed)
    else:
        _ok(f"Done in {elapsed:.1f}s")
        print_result(result, co.mode, final.iteration)


if __name__ == "__main__":
    main()
