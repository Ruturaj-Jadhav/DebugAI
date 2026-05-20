# DebugAI

An agentic debugging assistant for Java applications. Give it a repository and a bug report — stack trace, logs, or plain English — and it finds where the bug is.

```
$ python cli.py --repo ./spring-petclinic --error error.txt

[1/3] Reading error input
  ✓ 12 line(s) read

[2/3] Indexing repository
  ✓ 47 production classes indexed, 18 dependency edges

[3/3] Investigating
  ✓ Mode detected: DEVELOPER (confidence 85%)
  ✓ Done in 34.2s

══════════════════════════════════════════════════════════════
  DebugAI — Investigation Result
══════════════════════════════════════════════════════════════

  Location
  ────────────────────────────
  Class   : OwnerController
  Method  : findOwner()
  Line    : 66
  File    : src/main/java/.../owner/OwnerController.java

  Confidence  High (82%)
  The stack trace directly named this class and the null check
  absence was confirmed by reading the method body.

  Technical Summary
  ────────────────────────────
  ownerId is declared as Integer but no null check exists before
  calling ownerId.toString() at line 66. When the path variable
  is missing or malformed, this produces a NullPointerException.

  Business Summary
  ────────────────────────────
  The owner profile page crashes when accessed without a valid
  owner ID in the URL. A missing validation step is the cause.
══════════════════════════════════════════════════════════════
```

---

## Why Not Just Paste It Into ChatGPT?

This comes up every time. The honest answer:

**ChatGPT has no idea what your codebase looks like.**
Paste a stack trace and it gives generic NPE advice. DebugAI knows your actual class structure, endpoint mappings, and dependency graph. It finds `OwnerController.findOwner()` because it navigated your code — not because it guessed.

**You are doing the navigation work yourself with ChatGPT.**
Paste error → read response → manually open files → paste relevant code → repeat. That loop — figuring out *which files to open* — is the 30-60 minutes DebugAI eliminates.

**Business language to code location is unsolved elsewhere.**
If a PO says *"save is broken on the Trade Entry page"*, ChatGPT and Cursor have nothing to work with unless you already know which file handles that. DebugAI maps that description to `TradeController.saveTrade()` through the repo index. No manual navigation needed.

---

## How It Works

```
User Input (stack trace / logs / plain English)
          │
          ▼
    ┌─────────────┐
    │  Classifier  │  LLM extracts structured signals.
    │              │  Auto-detects developer vs business mode.
    └──────┬───────┘
           │
           ▼
    ┌─────────────┐
    │    Index     │  Tree-sitter parses every .java file once.
    │              │  Builds class map, endpoints, call graph.
    │              │  Cached to disk — fast on repeat runs.
    └──────┬───────┘
           │
           ▼
    ┌─────────────────────────────────────┐
    │         Investigator Agent           │
    │                                     │
    │  INTAKE → INVESTIGATE → TOOL CALL   │
    │               ↑            │        │
    │               └────────────┘        │
    │                    │                │
    │                 ROUTE               │
    │              ↙        ↘             │
    │         OUTPUT      FALLBACK        │
    └─────────────────────────────────────┘
           │
           ▼
    InvestigationResult
    (class · method · file · line · explanation)
```

The agent runs a hypothesis-driven loop. It starts broad (explore) — searching the index for candidate classes. Once it finds a strong suspect, it switches to deep (exploit) — reading actual method source to confirm. It stops when confident or when it runs out of iterations, always producing a result with an honest confidence score.

---

## What Makes It Non-Trivial

**Hypothesis tracking across iterations**
The agent doesn't just call tools randomly. It maintains a structured hypothesis — suspected class, method, evidence, what's missing — and updates it after every tool call. Each decision is guided by what the hypothesis still needs, not just what to do next.

**Explore / exploit strategy**
Two distinct investigation modes controlled by the ROUTE node. Explore = search broadly, find candidates. Exploit = go deep on one suspect. The agent switches automatically based on hypothesis confidence. If stuck (two consecutive empty results), it forces a switch back to explore.

**Business language → code location**
The hardest part. "Save broken on Trade Entry page" has no class names, no line numbers, nothing technical. The index's keyword scoring maps feature names and action verbs to endpoint URLs, controller handlers, and service methods. This works without any configuration on any Spring Boot repo.

**Bounded context window**
The index stores signatures not source code. The agent queries it like a database — three tools, one schema, no raw file reads during investigation. Method source is only loaded when the agent has already narrowed to a specific suspect. Context stays under 24k characters regardless of repo size.

---

## Setup

```bash
git clone https://github.com/your-username/debugai
cd debugai
pip install -r requirements.txt
```

Set your API key (free at [openrouter.ai](https://openrouter.ai)):

```bash
# Mac / Linux
export OPENROUTER_API_KEY=your_key

# Windows
set OPENROUTER_API_KEY=your_key
```

---

## Usage

```bash
# Basic — provide error as a file
python cli.py --repo ./spring-petclinic --error error.txt

# Paste error inline (no file needed)
python cli.py --repo ./spring-petclinic

# With package prefix — better frame filtering for large repos
python cli.py --repo ./spring-petclinic --error error.txt --package org.springframework.samples

# Force reindex after code changes
python cli.py --repo ./spring-petclinic --error error.txt --reindex

# JSON output for piping or integration
python cli.py --repo ./spring-petclinic --error error.txt --json
```

The error input accepts anything: a raw Java stack trace, application logs, a business description, or a mix.

---

## Model Configuration

DebugAI uses OpenRouter. The default model is:

```
openai/gpt-oss-120b:free
```

To use a different model, set the environment variable:

```bash
export DEBUGAI_MODEL=meta-llama/llama-3.3-70b-instruct:free
```

Any OpenRouter-compatible model works. Free tier is sufficient for most investigations.

---

## Project Structure

```
debugai/
├── cli.py                        ← entry point
├── requirements.txt
│
├── indexer/
│   ├── index_schema.py           ← RepoIndex, ClassIndex, query methods
│   ├── java_parser.py            ← tree-sitter AST → ClassIndex
│   └── index_builder.py          ← walks repo, builds + caches index
│
├── classifier/
│   ├── schema.py                 ← ClassifierOutput dataclasses
│   ├── prompt.py                 ← extraction prompt
│   └── classifier.py             ← LLM call → structured output
│
├── agent/
│   ├── state.py                  ← AgentState, Hypothesis, Strategy
│   ├── tools.py                  ← search_index, get_class_summary, read_method
│   ├── prompts.py                ← system prompt, investigate, output, fallback
│   ├── graph.py                  ← LangGraph wiring
│   └── nodes/
│       ├── intake.py             ← seeds investigation from classifier output
│       ├── investigate.py        ← LLM decides next tool
│       ├── tool_call.py          ← executes tool deterministically
│       ├── route.py              ← controls loop, strategy switching
│       ├── fallback.py           ← smart fallback on exhaustion
│       └── output.py             ← formats InvestigationResult
│
└── tests/
    ├── test_tools.py             ← deterministic tool tests (no LLM)
    └── test_agent.py             ← 3-layer: state, nodes, graph
```

---

## Limitations

- Java only (Python, Node support planned)
- Spring Boot focused — Quarkus and Jakarta EE partial support
- Small to medium repos tested (under 100k lines)
- Call graph is dependency-inferred not call-site parsed — exact method-to-method edges require AST body parsing (planned)
- Source files must be present locally — remote repos not yet supported

---

## License

MIT
