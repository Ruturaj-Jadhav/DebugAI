"""
index_schema.py — v3

Changelog v3:
  - ClassType: added TEST
  - EndpointInfo: base_path + relative_path + full_paths + handler_method_id
  - MethodInfo: removed empty `calls` field
  - DependencyEdge: class-level only (no method-to-method over-generation)
  - RepoIndex: schema_version="3", renamed find_callers/callees to find_dependencies_of/dependents_of
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

SCHEMA_VERSION = "3"


class ClassType(str, Enum):
    CONTROLLER  = "controller"
    SERVICE     = "service"
    REPOSITORY  = "repository"
    COMPONENT   = "component"
    CONFIG      = "config"
    ENTITY      = "entity"
    TEST        = "test"
    UNKNOWN     = "unknown"


class HttpMethod(str, Enum):
    GET     = "GET"
    POST    = "POST"
    PUT     = "PUT"
    DELETE  = "DELETE"
    PATCH   = "PATCH"
    ANY     = "ANY"


class EdgeKind(str, Enum):
    INFERRED_DEPENDENCY = "inferred_dependency"


@dataclass
class EndpointInfo:
    """
    HTTP endpoint on a controller method.

    Paths stored at three levels:
        base_path     = "/owners/{ownerId}"       from class @RequestMapping
        relative_path = "/pets/new"               from method @GetMapping
        full_paths    = ["/owners/{ownerId}/pets/new"]  joined (list for multi-path)

    handler_method_id links to exact MethodInfo:
        "initCreationForm(Owner,ModelMap)"
    """
    http_method:       HttpMethod
    base_path:         str
    relative_path:     str
    full_paths:        list[str]
    handler_name:      str
    handler_method_id: str
    line_number:       int
    parameters:        list[str] = field(default_factory=list)


@dataclass
class MethodInfo:
    """
    A method inside any Java class.
    method_id disambiguates overloads: "findAll()" vs "findAll(Pageable)"
    Note: calls field removed in v3 — returns in v4 from AST body parsing.
    """
    name:        str
    method_id:   str
    line_number: int
    return_type: str        = "void"
    parameters:  list[str] = field(default_factory=list)


@dataclass
class DependencyInfo:
    """
    An injected dependency field on a class.
    dependency_kind: "autowired" | "constructor" | "unknown"
    """
    class_name:      str
    field_name:      str
    dependency_kind: str = "autowired"


@dataclass
class ClassIndex:
    """
    Everything we know about a single Java class or interface.
    source_set: "main" | "test" | "other"
    class_type is forced to TEST when source_set == "test".
    """
    class_name:   str
    file_path:    str
    class_type:   ClassType
    package:      str                   = ""
    source_set:   str                   = "main"
    base_url:     str                   = ""
    endpoints:    list[EndpointInfo]    = field(default_factory=list)
    methods:      list[MethodInfo]      = field(default_factory=list)
    dependencies: list[DependencyInfo]  = field(default_factory=list)
    annotations:  list[str]            = field(default_factory=list)


@dataclass
class DependencyEdge:
    """
    Class-level dependency: caller_class depends on callee_class via field_name.
    Intentionally NOT method-level — that requires AST body parsing (v4).

    Example:
        OwnerController → OwnerRepository via "ownerRepository"
        Agent reads: "when debugging OwnerController, check OwnerRepository"
    """
    caller_class: str
    callee_class: str
    field_name:   str
    edge_kind:    str = EdgeKind.INFERRED_DEPENDENCY


@dataclass
class RepoIndex:
    """
    Complete index for a Java repo. Built once, cached to disk.
    schema_version triggers cache rebuild when schema changes.
    """
    repo_path:          str
    schema_version:     str                     = SCHEMA_VERSION
    classes:            list[ClassIndex]        = field(default_factory=list)
    dependency_edges:   list[DependencyEdge]    = field(default_factory=list)
    indexed_at:         str                     = ""

    def prod_classes(self) -> list[ClassIndex]:
        return [c for c in self.classes if c.source_set == "main"]

    def test_classes(self) -> list[ClassIndex]:
        return [c for c in self.classes if c.source_set == "test"]

    def find_by_endpoint(self, url_fragment: str) -> list[ClassIndex]:
        """Find controllers matching url_fragment across all full_paths."""
        results  = []
        fragment = url_fragment.lower()
        for cls in self.prod_classes():
            if cls.class_type != ClassType.CONTROLLER:
                continue
            for ep in cls.endpoints:
                if any(fragment in p.lower() for p in ep.full_paths):
                    results.append(cls)
                    break
        return results

    def find_by_class_name(self, name: str) -> Optional[ClassIndex]:
        for cls in self.classes:
            if cls.class_name == name:
                return cls
        return None

    def find_by_type(self, class_type: ClassType, prod_only: bool = True) -> list[ClassIndex]:
        source = self.prod_classes() if prod_only else self.classes
        return [c for c in source if c.class_type == class_type]

    def find_dependencies_of(self, class_name: str) -> list[DependencyEdge]:
        """What does class_name depend on? (outgoing edges)"""
        return [e for e in self.dependency_edges if e.caller_class == class_name]

    def find_dependents_of(self, class_name: str) -> list[DependencyEdge]:
        """Which classes depend on class_name? (incoming edges)"""
        return [e for e in self.dependency_edges if e.callee_class == class_name]

    def search_by_keyword(self, keyword: str, prod_only: bool = True) -> list[ClassIndex]:
        """
        Scored keyword search across class names, method names, endpoint paths.
        Business mode entry point: "Trade Entry" → TradeController, TradeService
        Scores: class name +3, full_path +2, handler/method name +1
        """
        keyword = keyword.lower()
        source  = self.prod_classes() if prod_only else self.classes
        results = []
        seen    = set()
        for cls in source:
            score = 0
            if keyword in cls.class_name.lower():
                score += 3
            for ep in cls.endpoints:
                if any(keyword in p.lower() for p in ep.full_paths):
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
