# DebugAI 🐛

An agentic debugging assistant for Java applications. Give it a bug report — in plain business language or a raw stack trace — and it investigates your codebase, traces the call chain, and tells you where the problem likely lives.

> **Status:** MVP in active development. Currently supports Java Spring Boot applications.

---

## The Problem This Solves

When a bug is reported in UAT or production, a developer typically spends 30–60 minutes just figuring out *where to look* before they can start fixing anything. This gap is worse when the report comes from a business user:

> "The save button is not working on the Trade Entry page."

No stack trace. No logs. No class names. Just a symptom.

DebugAI bridges the gap between a vague business report and the exact controller, service, and method where the failure is occurring.

---

## How It Works

DebugAI uses a **hybrid architecture** — deterministic at the edges, an LLM agent in the investigation core.

```
User Input (business description or stack trace)
        ↓
Input Parser
Classifies mode, extracts structured fields
        ↓
Repo Index (built once, cached on disk)
Structural map of all classes, endpoints, dependencies
        ↓
Investigator Agent (LLM)
Calls index query tools iteratively to narrow down location
        ↓
Output
Suspected class + method + plain English explanation
```

The repo is indexed **once** on first run and cached. Every debug session loads the cached index — the LLM never reads raw Java files directly. It navigates via typed query methods, then reads only the specific method bodies it needs.

---

## Key Design Decisions

**Index as navigation map, not content store**
The index stores class names, method signatures, endpoint URLs, and dependency relationships — not code. This keeps the LLM context window bounded regardless of repo size.

**Three tools only for the agent**
The investigator agent has exactly three tools: `search_by_keyword`, `get_class_summary`, and `read_method`. Fewer tools means fewer decision points and lower hallucination risk.

**Structural call graph inference**
The call graph is built from `@Autowired` dependency relationships, not method body parsing. If `TradeController` injects `TradeService`, we infer a call relationship. This is intentionally a structural approximation — good enough for navigation, fast to compute.

**Business mode vs Developer mode**
The user explicitly selects their mode. No classifier guessing. Business mode takes plain English and maps it to code via endpoint and keyword search. Developer mode takes a stack trace, filters framework noise, and maps frames to your application code.

---

## Architecture Deep Dive

### Component 1 — Repo Indexer

The indexer is the foundation of the system. It runs once per repo and produces a structured JSON index that everything else reads from.

#### What It Indexes

For every `.java` file in the repo the indexer extracts:

- **Controllers** (`@RestController`, `@Controller`) — class name, base URL from `@RequestMapping`, every HTTP endpoint with its method, URL path, handler name, and line number
- **Services** (`@Service`) — class name, all public method names and line numbers, `@Autowired` dependencies
- **Repositories** (`@Repository`) — class name, entity managed, dependencies
- **Components** (`@Component`) — class name, methods, dependencies
- **Call graph** — directed edges between classes inferred from dependency injection

#### How It Works

Tree-sitter parses each `.java` file into an Abstract Syntax Tree (AST). We query the AST for Spring annotations, class declarations, method declarations, and field declarations. This is more reliable than regex — it handles multi-line annotations, nested generics, unusual formatting, and comments inside code correctly.

```
TradeController.java
        ↓ tree-sitter AST
        ↓ annotation extraction  →  @RestController, @RequestMapping("/api/trade")
        ↓ method extraction      →  saveTrade() POST /api/trade/save line 14
        ↓ dependency extraction  →  @Autowired TradeService tradeService
        ↓
ClassIndex { class_name, file_path, class_type, endpoints, methods, dependencies }
```

#### Index Schema

```
RepoIndex
├── repo_path
├── indexed_at  (ISO timestamp)
├── classes: List[ClassIndex]
│   ├── class_name, file_path, class_type, package
│   ├── base_url  (controllers only)
│   ├── endpoints: List[EndpointInfo]
│   │   └── http_method, url_path, handler_name, line_number, parameters
│   ├── methods: List[MethodInfo]
│   │   └── name, line_number, return_type, parameters
│   └── dependencies: List[DependencyInfo]
│       └── class_name, field_name
└── call_edges: List[CallEdge]
    └── caller_class, caller_method, callee_class, callee_method
```

#### Query Methods

The investigator agent interacts with the index through five typed query methods. It never touches raw files during a debug session.

| Method | Description | Used By |
|---|---|---|
| `find_by_endpoint(url_fragment)` | Find controllers whose endpoints match a URL fragment | Business mode |
| `find_by_class_name(name)` | Exact class name lookup | Developer mode |
| `find_by_type(class_type)` | All controllers, services, or repositories | Both |
| `find_callers(class, method)` | Who calls this method? | Both |
| `find_callees(class, method)` | What does this method depend on? | Both |
| `search_by_keyword(keyword)` | Fuzzy search across class names, method names, URLs | Business mode |

`search_by_keyword` is the bridge between plain English and code. It scores matches across class names, method names, and endpoint URLs — so "Trade Entry" finds `TradeController`, `TradeService`, and any endpoint containing `/trade`.

#### Caching

The index is saved as `.debugai_index.json` at the repo root after the first scan. Subsequent runs load from cache instantly. Rebuild with `--reindex`.

```bash
# First run — scans and indexes
debugai --repo ./my-project --mode business

# Subsequent runs — loads cache
debugai --repo ./my-project --mode business

# Force rebuild
debugai --repo ./my-project --reindex
```

#### What Gets Skipped

The following directories are excluded from scanning:

- `target/` — Maven build output
- `build/` — Gradle build output
- `.git/`, `.idea/` — VCS and IDE files
- `generated-sources/` — auto-generated code

Files that fail to parse are skipped with a warning. The indexer never crashes on a single bad file.

---

### Component 2 — Input Parser *(in progress)*

Two paths based on user-selected mode.

**Business mode** — LLM extracts feature name, action, symptom, and any entity identifiers from plain English. Output is passed to the index as a keyword search.

**Developer mode** — Regex-based stack trace parser filters out framework noise (`org.springframework.*`, `java.*`, `sun.*`) and extracts only your application package frames. Those class names are passed directly to the index for lookup.

---

### Component 3 — Investigator Agent *(planned)*

LangGraph-based agent with three tools:

- `search_by_keyword` / `find_by_endpoint` — index navigation
- `get_class_summary` — method names and signatures for a class (no code)
- `read_method` — full body of a single method on demand

The agent loops until it reaches a confident hypothesis or hits the maximum iteration guard (default: 5 iterations). It is grounded by the index but not constrained — if evidence points elsewhere, it follows the evidence.

---

### Component 4 — Output Formatter *(planned)*

Produces two sections in every response:

- **Technical** — suspected class, method, line number, call chain
- **Plain English** — what likely happened and why, readable by a business user

---

## Project Structure

```
debugai/
├── indexer/
│   ├── index_schema.py       # Data contracts — ClassIndex, RepoIndex, query methods
│   ├── java_parser.py        # Tree-sitter parser — .java file → ClassIndex
│   └── index_builder.py      # Repo walker — builds, saves, loads RepoIndex
├── parser/
│   └── input_parser.py       # Input classifier and field extractor (in progress)
├── agent/
│   └── investigator.py       # LangGraph investigator agent (planned)
├── output/
│   └── formatter.py          # Output formatter (planned)
├── tests/
│   └── sample_repo/          # Sample Spring Boot repo for testing
├── cli.py                    # Entry point
└── requirements.txt
```

---

## Getting Started

### Requirements

- Python 3.11+
- A Java Spring Boot repository to analyse

### Installation

```bash
git clone https://github.com/your-username/debugai
cd debugai
pip install -r requirements.txt
```

### Requirements file

```
tree-sitter
tree-sitter-java
langchain
langgraph
groq
```

### Set your LLM API key

DebugAI uses Groq by default (free tier, no credit card required).

```bash
export GROQ_API_KEY=your_key_here
```

Get a free key at [console.groq.com](https://console.groq.com).

### Run

```bash
# Business mode
python cli.py --repo /path/to/java/project --mode business

# Developer mode (paste stack trace when prompted)
python cli.py --repo /path/to/java/project --mode developer

# Force reindex
python cli.py --repo /path/to/java/project --reindex
```

---

## Example Session

```
$ python cli.py --repo ./trade-app --mode business

[indexer] Scanning /trade-app
[indexer] Found 42 .java files
[indexer] Parsed 38 classes
[indexer] Built call graph: 94 edges
[indexer] Index saved to .debugai_index.json

Describe the issue:
> Save not working on Trade Entry page

[agent] Searching for: trade, save
[agent] Found: TradeController, TradeService, TradeRepository
[agent] Checking endpoint: POST /api/trade/save → saveTrade() line 14
[agent] Tracing: TradeController.saveTrade → TradeService.save
[agent] Reading: TradeService.save()

── Result ──────────────────────────────────────────────
Suspected location:  TradeService.java → save() method, line 38
Caller:              TradeController.saveTrade() line 14

The save() method calls tradeValidator.validate(trade) before
persisting. If the Trade object arrives with a null contract field,
validate() likely throws an unchecked exception that is not handled
by the controller — causing the save to silently fail from the
user's perspective.

Suggested next step: Check null handling in TradeService.save()
and verify what TradeController passes before calling save().
────────────────────────────────────────────────────────
```

---

## Limitations (MVP)

- Java only (Python, Node.js support planned for v2)
- Spring Boot focused — Quarkus and Jakarta EE support is partial
- Call graph is structural (dependency-inferred), not behavioural (call-site parsed)
- Small to medium repos (under 100k lines) tested
- Single log file input only — multi-service log correlation is v2
- Local CLI only — no IDE plugin, no CI/CD integration

---

## Roadmap

- [ ] Input parser — business mode LLM extraction
- [ ] Input parser — stack trace noise filter
- [ ] Investigator agent — LangGraph implementation
- [ ] Output formatter — dual technical/business view
- [ ] Tree-sitter method body parsing for true call graph
- [ ] Python repo support
- [ ] Multi-log file correlation
- [ ] VS Code extension

---

## Why Not Just Use Cursor or GitHub Copilot?

Cursor is a brilliant assistant but it needs you to bring it to the right file. If you open `TradeService.java` and ask what's wrong, it will help. But it cannot take "save not working on Trade Entry page" and find `TradeService.java` on its own — that navigation step is on you.

DebugAI automates that navigation step. The 30-60 minutes of *figuring out where to look* is exactly what it targets.

---

## Contributing

This is an open source project built as a learning exercise in agentic system design. Contributions, issues, and architecture discussions are welcome.

---

## License

MIT