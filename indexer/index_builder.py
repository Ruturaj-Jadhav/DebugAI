"""
index_builder.py — v3

Fixes in v3:
  - DependencyEdge is now class-level only (caller_class → callee_class + field_name)
  - No more method-to-method over-generation or duplicates
  - schema_version check on cache load triggers rebuild if stale
  - Serialisation updated for all v3 schema fields
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .java_parser import parse_java_file
from .index_schema import (
    RepoIndex, ClassIndex, ClassType, DependencyEdge,
    EndpointInfo, MethodInfo, DependencyInfo, HttpMethod,
    SCHEMA_VERSION
)

_INDEX_FILENAME = ".debugai_index.json"


class IndexBuilder:

    def __init__(self, repo_path: str):
        self.repo_path  = str(Path(repo_path).resolve())
        self.index_file = str(Path(self.repo_path) / _INDEX_FILENAME)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> RepoIndex:
        """Full scan, parse, build, save. Always rebuilds."""
        print(f"[indexer] Scanning {self.repo_path}")

        java_files = self._find_java_files()
        print(f"[indexer] Found {len(java_files)} .java files")

        classes = self._parse_all(java_files)
        prod    = sum(1 for c in classes if c.source_set == "main")
        tests   = sum(1 for c in classes if c.source_set == "test")
        print(f"[indexer] Parsed {len(classes)} classes ({prod} prod, {tests} test)")

        # Class type breakdown
        from collections import Counter
        type_counts = Counter(c.class_type.value for c in classes if c.source_set == "main")
        print(f"[indexer] Prod types: {dict(type_counts)}")

        dep_edges = self._build_dependency_edges(classes)
        print(f"[indexer] Built {len(dep_edges)} class-level dependency edges")

        index = RepoIndex(
            repo_path        = self.repo_path,
            schema_version   = SCHEMA_VERSION,
            classes          = classes,
            dependency_edges = dep_edges,
            indexed_at       = datetime.now(timezone.utc).isoformat(),
        )

        self._save(index)
        print(f"[indexer] Saved -> {self.index_file}")
        return index

    def load(self) -> RepoIndex | None:
        if not Path(self.index_file).exists():
            return None
        return self._load()

    def get_or_build(self) -> RepoIndex:
        """Load from cache if schema matches, else rebuild."""
        cached = self.load()
        if cached is not None:
            if cached.schema_version != SCHEMA_VERSION:
                print(f"[indexer] Cache schema v{cached.schema_version} → current v{SCHEMA_VERSION}, rebuilding...")
                return self.build()
            prod  = sum(1 for c in cached.classes if c.source_set == "main")
            tests = sum(1 for c in cached.classes if c.source_set == "test")
            print(f"[indexer] Cache loaded ({cached.indexed_at})")
            print(f"[indexer] {len(cached.classes)} classes ({prod} prod, {tests} test), {len(cached.dependency_edges)} edges")
            return cached
        return self.build()

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _find_java_files(self) -> list[str]:
        skip_dirs = {
            "target", "build", ".git", ".idea",
            "node_modules", "out", "generated-sources"
        }
        java_files = []
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                if file.endswith(".java"):
                    java_files.append(os.path.join(root, file))
        return sorted(java_files)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_all(self, java_files: list[str]) -> list[ClassIndex]:
        classes = []
        failed  = 0
        for file_path in java_files:
            try:
                result = parse_java_file(file_path)
                if result is not None:
                    classes.append(result)
            except Exception as e:
                failed += 1
                print(f"[indexer] WARN: Could not parse {Path(file_path).name}: {e}")
        if failed:
            print(f"[indexer] {failed} files skipped due to parse errors")
        return classes

    # ------------------------------------------------------------------
    # Dependency edges — CLASS LEVEL ONLY
    # ------------------------------------------------------------------

    def _build_dependency_edges(self, classes: list[ClassIndex]) -> list[DependencyEdge]:
        """
        One edge per (caller_class, callee_class, field_name) tuple.
        Production classes only — test dependencies excluded.
        No method-to-method inference — that is v4 (AST body parsing).
        """
        prod_classes = [c for c in classes if c.source_set == "main"]
        class_names  = {cls.class_name for cls in prod_classes}
        edges        = []
        seen         = set()

        for cls in prod_classes:
            for dep in cls.dependencies:
                # Only link to classes that exist in this repo
                if dep.class_name not in class_names:
                    continue
                key = (cls.class_name, dep.class_name, dep.field_name)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(DependencyEdge(
                    caller_class = cls.class_name,
                    callee_class = dep.class_name,
                    field_name   = dep.field_name,
                ))

        return edges

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def _save(self, index: RepoIndex) -> None:
        data = _serialise(index)
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load(self) -> RepoIndex:
        with open(self.index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _deserialise(data)


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------

def _serialise(index: RepoIndex) -> dict:
    return {
        "schema_version":   index.schema_version,
        "repo_path":        index.repo_path,
        "indexed_at":       index.indexed_at,
        "classes":          [_serialise_class(c) for c in index.classes],
        "dependency_edges": [
            {
                "caller_class": e.caller_class,
                "callee_class": e.callee_class,
                "field_name":   e.field_name,
                "edge_kind":    e.edge_kind,
            }
            for e in index.dependency_edges
        ],
    }


def _serialise_class(cls: ClassIndex) -> dict:
    return {
        "class_name":   cls.class_name,
        "file_path":    cls.file_path,
        "class_type":   cls.class_type.value,
        "package":      cls.package,
        "source_set":   cls.source_set,
        "base_url":     cls.base_url,
        "annotations":  cls.annotations,
        "endpoints": [
            {
                "http_method":       ep.http_method.value,
                "base_path":         ep.base_path,
                "relative_path":     ep.relative_path,
                "full_paths":        ep.full_paths,
                "handler_name":      ep.handler_name,
                "handler_method_id": ep.handler_method_id,
                "line_number":       ep.line_number,
                "parameters":        ep.parameters,
            }
            for ep in cls.endpoints
        ],
        "methods": [
            {
                "name":        m.name,
                "method_id":   m.method_id,
                "line_number": m.line_number,
                "return_type": m.return_type,
                "parameters":  m.parameters,
            }
            for m in cls.methods
        ],
        "dependencies": [
            {
                "class_name":      d.class_name,
                "field_name":      d.field_name,
                "dependency_kind": d.dependency_kind,
            }
            for d in cls.dependencies
        ],
    }


def _deserialise(data: dict) -> RepoIndex:
    classes = [_deserialise_class(c) for c in data.get("classes", [])]
    edges = [
        DependencyEdge(
            caller_class = e["caller_class"],
            callee_class = e["callee_class"],
            field_name   = e["field_name"],
            edge_kind    = e.get("edge_kind", "inferred_dependency"),
        )
        for e in data.get("dependency_edges", [])
    ]
    return RepoIndex(
        schema_version   = data.get("schema_version", "1"),
        repo_path        = data["repo_path"],
        indexed_at       = data.get("indexed_at", ""),
        classes          = classes,
        dependency_edges = edges,
    )


def _deserialise_class(data: dict) -> ClassIndex:
    return ClassIndex(
        class_name  = data["class_name"],
        file_path   = data["file_path"],
        class_type  = ClassType(data["class_type"]),
        package     = data.get("package", ""),
        source_set  = data.get("source_set", "main"),
        base_url    = data.get("base_url", ""),
        annotations = data.get("annotations", []),
        endpoints=[
            EndpointInfo(
                http_method       = HttpMethod(ep["http_method"]),
                base_path         = ep.get("base_path", ""),
                relative_path     = ep.get("relative_path", ""),
                full_paths        = ep.get("full_paths", [ep.get("url_path", "/")]),
                handler_name      = ep["handler_name"],
                handler_method_id = ep.get("handler_method_id", ep["handler_name"] + "()"),
                line_number       = ep["line_number"],
                parameters        = ep.get("parameters", []),
            )
            for ep in data.get("endpoints", [])
        ],
        methods=[
            MethodInfo(
                name        = m["name"],
                method_id   = m.get("method_id", m["name"] + "()"),
                line_number = m["line_number"],
                return_type = m.get("return_type", "void"),
                parameters  = m.get("parameters", []),
            )
            for m in data.get("methods", [])
        ],
        dependencies=[
            DependencyInfo(
                class_name      = d["class_name"],
                field_name      = d["field_name"],
                dependency_kind = d.get("dependency_kind", "autowired"),
            )
            for d in data.get("dependencies", [])
        ],
    )
