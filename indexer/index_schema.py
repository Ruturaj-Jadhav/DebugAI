"""
index_schema.py

Core data contracts for the DebugAI indexer.
Everything downstream — the investigator agent, the input parser, the output formatter —
reads from these structures. Change these carefully.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ClassType(str, Enum):
    CONTROLLER  = "controller"
    SERVICE     = "service"
    REPOSITORY  = "repository"
    COMPONENT   = "component"   # @Component — generic Spring bean
    UNKNOWN     = "unknown"     # Java class with no recognised Spring annotation


class HttpMethod(str, Enum):
    GET     = "GET"
    POST    = "POST"
    PUT     = "PUT"
    DELETE  = "DELETE"
    PATCH   = "PATCH"
    ANY     = "ANY"             # @RequestMapping with no method specified


# ---------------------------------------------------------------------------
# Fine-grained building blocks
# ---------------------------------------------------------------------------

@dataclass
class EndpointInfo:
    """
    A single HTTP endpoint exposed by a controller method.

    Example
    -------
    @PostMapping("/save")
    public ResponseEntity<Trade> saveTrade(...) { ... }

    maps to:
        http_method  = HttpMethod.POST
        url_path     = "/api/trade/save"   (base_url + method_path combined)
        handler_name = "saveTrade"
        line_number  = 42
    """
    http_method:  HttpMethod
    url_path:     str                       # full path including controller base
    handler_name: str                       # the Java method name
    line_number:  int
    parameters:   list[str] = field(default_factory=list)   # raw param type names


@dataclass
class MethodInfo:
    """
    A single method inside any Java class.
    Used for services, repositories, and non-endpoint controller methods.
    """
    name:         str
    line_number:  int
    return_type:  str                       = "void"
    parameters:   list[str]                = field(default_factory=list)
    calls:        list[str]                = field(default_factory=list)
    # calls: list of "ClassName.methodName" strings this method invokes
    # populated during call graph building — empty at parse time


@dataclass
class DependencyInfo:
    """
    A field-level dependency injected via @Autowired or constructor injection.

    Example
    -------
    @Autowired
    private TradeService tradeService;

    maps to:
        class_name  = "TradeService"
        field_name  = "tradeService"
    """
    class_name:  str
    field_name:  str


# ---------------------------------------------------------------------------
# Per-class index entry
# ---------------------------------------------------------------------------

@dataclass
class ClassIndex:
    """
    Everything we know about a single Java class.
    One ClassIndex per .java file (we assume one top-level class per file,
    which is standard Java convention).
    """
    class_name:   str
    file_path:    str                       # absolute path on disk
    class_type:   ClassType
    package:      str                       = ""

    # Controller-specific
    base_url:     str                       = ""    # from @RequestMapping at class level
    endpoints:    list[EndpointInfo]        = field(default_factory=list)

    # All classes
    methods:      list[MethodInfo]          = field(default_factory=list)
    dependencies: list[DependencyInfo]      = field(default_factory=list)
    # dependencies = @Autowired / constructor-injected fields

    annotations:  list[str]                = field(default_factory=list)
    # raw annotation names e.g. ["RestController", "RequestMapping"]


# ---------------------------------------------------------------------------
# Call graph
# ---------------------------------------------------------------------------

@dataclass
class CallEdge:
    """
    A directed edge in the call graph.

    caller_class.caller_method → callee_class.callee_method

    Example:
        TradeController.saveTrade → TradeService.save
    """
    caller_class:  str
    caller_method: str
    callee_class:  str
    callee_method: str


# ---------------------------------------------------------------------------
# Top-level repo index  (this is what gets saved to disk as JSON)
# ---------------------------------------------------------------------------

@dataclass
class RepoIndex:
    """
    The complete index for a single Java repository.
    Built once, cached to disk, queried many times.

    Query patterns the investigator agent will use:
        - find_by_endpoint("/api/trade/save")   → ClassIndex (controller)
        - find_by_class_name("TradeService")    → ClassIndex
        - find_callers("TradeService", "save")  → list[CallEdge]
        - find_callees("TradeController", "saveTrade") → list[CallEdge]
        - search_by_keyword("trade")            → list[ClassIndex]
    """
    repo_path:      str
    classes:        list[ClassIndex]    = field(default_factory=list)
    call_edges:     list[CallEdge]      = field(default_factory=list)
    indexed_at:     str                 = ""    # ISO timestamp, set by index_builder

    # ---------------------------------------------------------------------------
    # Query helpers — used by the investigator agent, not by the parser
    # ---------------------------------------------------------------------------

    def find_by_endpoint(self, url_fragment: str) -> list[ClassIndex]:
        """
        Return controllers whose endpoints contain url_fragment.
        Case-insensitive partial match.

        find_by_endpoint("trade/save") matches "/api/trade/save"
        """
        results = []
        fragment = url_fragment.lower()
        for cls in self.classes:
            if cls.class_type != ClassType.CONTROLLER:
                continue
            for ep in cls.endpoints:
                if fragment in ep.url_path.lower():
                    results.append(cls)
                    break
        return results

    def find_by_class_name(self, name: str) -> Optional[ClassIndex]:
        """Exact class name lookup. Returns first match."""
        for cls in self.classes:
            if cls.class_name == name:
                return cls
        return None

    def find_by_type(self, class_type: ClassType) -> list[ClassIndex]:
        """Return all classes of a given type."""
        return [c for c in self.classes if c.class_type == class_type]

    def find_callers(self, class_name: str, method_name: str) -> list[CallEdge]:
        """Who calls class_name.method_name?"""
        return [
            e for e in self.call_edges
            if e.callee_class == class_name and e.callee_method == method_name
        ]

    def find_callees(self, class_name: str, method_name: str) -> list[CallEdge]:
        """What does class_name.method_name call?"""
        return [
            e for e in self.call_edges
            if e.caller_class == class_name and e.caller_method == method_name
        ]

    def search_by_keyword(self, keyword: str) -> list[ClassIndex]:
        """
        Fuzzy keyword search across class names, method names, and endpoint URLs.
        Used by business mode — 'Trade Entry' → finds TradeController, TradeService.
        """
        keyword = keyword.lower()
        results = []
        seen = set()
        for cls in self.classes:
            score = 0
            if keyword in cls.class_name.lower():
                score += 3
            for ep in cls.endpoints:
                if keyword in ep.url_path.lower():
                    score += 2
                if keyword in ep.handler_name.lower():
                    score += 1
            for m in cls.methods:
                if keyword in m.name.lower():
                    score += 1
            if score > 0 and cls.class_name not in seen:
                results.append((score, cls))
                seen.add(cls.class_name)
        results.sort(key=lambda x: x[0], reverse=True)
        return [cls for _, cls in results]
