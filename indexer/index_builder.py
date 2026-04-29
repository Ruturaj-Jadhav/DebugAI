"""
index_builder.py

Orchestrates the full repo indexing process.

Responsibilities:
  1. Walk the repo directory recursively
  2. Find all .java files
  3. Parse each with java_parser
  4. Build call graph from dependency info
  5. Save RepoIndex to disk as JSON
  6. Load cached index on subsequent runs

Usage:
    from indexer.index_builder import IndexBuilder

    builder = IndexBuilder("/path/to/repo")
    index = builder.build()           # full scan + save
    index = builder.load()            # load from cache
    index = builder.get_or_build()    # load if fresh, build if stale
"""

import json
import os
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

from .java_parser import parse_java_file
from .index_schema import (
    RepoIndex, ClassIndex, ClassType, CallEdge,
    EndpointInfo, MethodInfo, DependencyInfo, HttpMethod
)

# Cache file sits at repo root
_INDEX_FILENAME = ".debugai_index.json"


class IndexBuilder:

    def __init__(self, repo_path: str):
        self.repo_path  = str(Path(repo_path).resolve())
        self.index_file = str(Path(self.repo_path) / _INDEX_FILENAME)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> RepoIndex:
        """
        Full scan: walk every .java file, parse it, build index, save to disk.
        Always rebuilds even if cache exists.
        """
        print(f"[indexer] Scanning {self.repo_path}")

        java_files = self._find_java_files()
        print(f"[indexer] Found {len(java_files)} .java files")

        classes = self._parse_all(java_files)
        print(f"[indexer] Parsed {len(classes)} classes")

        call_edges = self._build_call_graph(classes)
        print(f"[indexer] Built call graph: {len(call_edges)} edges")

        index = RepoIndex(
            repo_path   = self.repo_path,
            classes     = classes,
            call_edges  = call_edges,
            indexed_at  = datetime.now(timezone.utc).isoformat(),
        )

        self._save(index)
        print(f"[indexer] Index saved to {self.index_file}")

        return index

    def load(self) -> RepoIndex | None:
        """
        Load index from disk cache.
        Returns None if no cache exists.
        """
        if not Path(self.index_file).exists():
            return None
        return self._load()

    def get_or_build(self) -> RepoIndex:
        """
        Load from cache if it exists, otherwise build fresh.
        This is what the CLI will call on startup.
        """
        cached = self.load()
        if cached is not None:
            print(f"[indexer] Loaded cached index ({cached.indexed_at})")
            print(f"[indexer] {len(cached.classes)} classes, {len(cached.call_edges)} call edges")
            return cached
        return self.build()

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _find_java_files(self) -> list[str]:
        """
        Recursively find all .java files in the repo.
        Skips common non-source directories.
        """
        skip_dirs = {
            "target", "build", ".git", ".idea",
            "node_modules", "out", "generated-sources"
        }
        java_files = []

        for root, dirs, files in os.walk(self.repo_path):
            # Prune directories we should never scan
            dirs[:] = [d for d in dirs if d not in skip_dirs]

            for file in files:
                if file.endswith(".java"):
                    java_files.append(os.path.join(root, file))

        return sorted(java_files)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_all(self, java_files: list[str]) -> list[ClassIndex]:
        """
        Parse all java files. Skip files that fail or return None.
        """
        classes = []
        failed  = 0

        for file_path in java_files:
            try:
                result = parse_java_file(file_path)
                if result is not None:
                    classes.append(result)
            except Exception as e:
                failed += 1
                print(f"[indexer] WARN: Could not parse {file_path}: {e}")

        if failed:
            print(f"[indexer] {failed} files could not be parsed and were skipped")

        return classes

    # ------------------------------------------------------------------
    # Call graph
    # ------------------------------------------------------------------

    def _build_call_graph(self, classes: list[ClassIndex]) -> list[CallEdge]:
        """
        Build call edges from dependency relationships.

        Strategy:
          For each class, look at its @Autowired dependencies.
          For each dependency, find the matching ClassIndex.
          For each method in the current class, link it to the
          public methods of the dependency.

        This is a structural inference — we don't parse method bodies
        to find actual call sites. Instead we infer: if TradeController
        has TradeService injected, then TradeController's methods
        CAN call TradeService's methods.

        True call-site parsing requires method body analysis and is v2.
        For MVP this gives us the dependency chain which is what matters.
        """
        # Build lookup map: class_name -> ClassIndex
        class_map = {cls.class_name: cls for cls in classes}

        edges = []

        for cls in classes:
            for dep in cls.dependencies:
                dep_class = class_map.get(dep.class_name)
                if dep_class is None:
                    # Dependency not in repo (external library etc) — skip
                    continue

                # Link each method in this class to each method in dependency
                # For controllers: use endpoint handler names
                caller_methods = (
                    [ep.handler_name for ep in cls.endpoints]
                    + [m.name for m in cls.methods]
                )

                callee_methods = (
                    [m.name for m in dep_class.methods]
                    + [ep.handler_name for ep in dep_class.endpoints]
                )

                for caller_method in caller_methods:
                    for callee_method in callee_methods:
                        edges.append(CallEdge(
                            caller_class  = cls.class_name,
                            caller_method = caller_method,
                            callee_class  = dep_class.class_name,
                            callee_method = callee_method,
                        ))

        return edges

    # ------------------------------------------------------------------
    # Serialisation — save and load
    # ------------------------------------------------------------------

    def _save(self, index: RepoIndex) -> None:
        """Serialise RepoIndex to JSON and write to disk."""
        data = _serialise(index)
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load(self) -> RepoIndex:
        """Load RepoIndex from JSON cache on disk."""
        with open(self.index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _deserialise(data)


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------

def _serialise(index: RepoIndex) -> dict:
    """Convert RepoIndex to a plain dict for JSON serialisation."""
    return {
        "repo_path":  index.repo_path,
        "indexed_at": index.indexed_at,
        "classes":    [_serialise_class(c) for c in index.classes],
        "call_edges": [
            {
                "caller_class":  e.caller_class,
                "caller_method": e.caller_method,
                "callee_class":  e.callee_class,
                "callee_method": e.callee_method,
            }
            for e in index.call_edges
        ],
    }


def _serialise_class(cls: ClassIndex) -> dict:
    return {
        "class_name":   cls.class_name,
        "file_path":    cls.file_path,
        "class_type":   cls.class_type.value,
        "package":      cls.package,
        "base_url":     cls.base_url,
        "annotations":  cls.annotations,
        "endpoints": [
            {
                "http_method":  ep.http_method.value,
                "url_path":     ep.url_path,
                "handler_name": ep.handler_name,
                "line_number":  ep.line_number,
                "parameters":   ep.parameters,
            }
            for ep in cls.endpoints
        ],
        "methods": [
            {
                "name":        m.name,
                "line_number": m.line_number,
                "return_type": m.return_type,
                "parameters":  m.parameters,
                "calls":       m.calls,
            }
            for m in cls.methods
        ],
        "dependencies": [
            {
                "class_name": d.class_name,
                "field_name": d.field_name,
            }
            for d in cls.dependencies
        ],
    }


def _deserialise(data: dict) -> RepoIndex:
    classes = [_deserialise_class(c) for c in data.get("classes", [])]
    call_edges = [
        CallEdge(
            caller_class  = e["caller_class"],
            caller_method = e["caller_method"],
            callee_class  = e["callee_class"],
            callee_method = e["callee_method"],
        )
        for e in data.get("call_edges", [])
    ]
    return RepoIndex(
        repo_path  = data["repo_path"],
        indexed_at = data.get("indexed_at", ""),
        classes    = classes,
        call_edges = call_edges,
    )


def _deserialise_class(data: dict) -> ClassIndex:
    return ClassIndex(
        class_name  = data["class_name"],
        file_path   = data["file_path"],
        class_type  = ClassType(data["class_type"]),
        package     = data.get("package", ""),
        base_url    = data.get("base_url", ""),
        annotations = data.get("annotations", []),
        endpoints=[
            EndpointInfo(
                http_method  = HttpMethod(ep["http_method"]),
                url_path     = ep["url_path"],
                handler_name = ep["handler_name"],
                line_number  = ep["line_number"],
                parameters   = ep.get("parameters", []),
            )
            for ep in data.get("endpoints", [])
        ],
        methods=[
            MethodInfo(
                name        = m["name"],
                line_number = m["line_number"],
                return_type = m.get("return_type", "void"),
                parameters  = m.get("parameters", []),
                calls       = m.get("calls", []),
            )
            for m in data.get("methods", [])
        ],
        dependencies=[
            DependencyInfo(
                class_name = d["class_name"],
                field_name = d["field_name"],
            )
            for d in data.get("dependencies", [])
        ],
    )
