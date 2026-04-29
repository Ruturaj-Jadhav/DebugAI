"""
java_parser.py

Parses a single .java file using tree-sitter and returns a ClassIndex.
This is the only file in the project that knows about tree-sitter.
Everything else works with ClassIndex objects.

Supports:
  - Spring @RestController / @Controller
  - Spring @Service
  - Spring @Repository
  - Spring @Component
  - @Autowired field injection
  - @RequestMapping, @GetMapping, @PostMapping, @PutMapping,
    @DeleteMapping, @PatchMapping
  - Package and class name extraction
  - Method name + line number extraction
"""

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Node
from pathlib import Path

from .index_schema import (
    ClassIndex, ClassType, EndpointInfo, MethodInfo,
    DependencyInfo, HttpMethod
)

# ---------------------------------------------------------------------------
# Module-level parser — initialised once, reused for every file
# ---------------------------------------------------------------------------
_JAVA_LANGUAGE = Language(tsjava.language())
_PARSER = Parser(_JAVA_LANGUAGE)

# ---------------------------------------------------------------------------
# Annotation → ClassType mapping
# ---------------------------------------------------------------------------
_CLASS_TYPE_MAP = {
    "RestController": ClassType.CONTROLLER,
    "Controller":     ClassType.CONTROLLER,
    "Service":        ClassType.SERVICE,
    "Repository":     ClassType.REPOSITORY,
    "Component":      ClassType.COMPONENT,
}

# ---------------------------------------------------------------------------
# Annotation → HttpMethod mapping
# ---------------------------------------------------------------------------
_HTTP_METHOD_MAP = {
    "GetMapping":     HttpMethod.GET,
    "PostMapping":    HttpMethod.POST,
    "PutMapping":     HttpMethod.PUT,
    "DeleteMapping":  HttpMethod.DELETE,
    "PatchMapping":   HttpMethod.PATCH,
    "RequestMapping": HttpMethod.ANY,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_java_file(file_path: str) -> ClassIndex | None:
    """
    Parse a single .java file and return a ClassIndex.
    Returns None if the file has no recognisable class declaration.
    """
    source = Path(file_path).read_bytes()
    tree = _PARSER.parse(source)
    root = tree.root_node

    package = _extract_package(root, source)
    class_node = _find_class_declaration(root)

    if class_node is None:
        return None

    class_name  = _get_class_name(class_node, source)
    annotations = _get_class_annotations(class_node, source)
    class_type  = _resolve_class_type(annotations)
    base_url    = _get_base_url(class_node, source)

    methods      = []
    endpoints    = []
    dependencies = []

    for member in _get_class_body_members(class_node):
        if member.type == "method_declaration":
            method_annotations = _get_node_annotations(member, source)
            method_name        = _get_identifier(member, source)
            line_number        = member.start_point[0] + 1  # tree-sitter is 0-indexed
            params             = _get_parameter_types(member, source)
            return_type        = _get_return_type(member, source)

            # Check if this method is an HTTP endpoint
            http_method, path = _resolve_endpoint(method_annotations, source, member)
            if http_method is not None:
                full_path = _join_paths(base_url, path)
                endpoints.append(EndpointInfo(
                    http_method  = http_method,
                    url_path     = full_path,
                    handler_name = method_name,
                    line_number  = line_number,
                    parameters   = params,
                ))
            else:
                methods.append(MethodInfo(
                    name        = method_name,
                    line_number = line_number,
                    return_type = return_type,
                    parameters  = params,
                ))

        elif member.type == "field_declaration":
            dep = _extract_dependency(member, source)
            if dep:
                dependencies.append(dep)

    return ClassIndex(
        class_name   = class_name,
        file_path    = str(Path(file_path).resolve()),
        class_type   = class_type,
        package      = package,
        base_url     = base_url,
        endpoints    = endpoints,
        methods      = methods,
        dependencies = dependencies,
        annotations  = annotations,
    )


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

def _extract_package(root: Node, source: bytes) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    return source[sub.start_byte:sub.end_byte].decode()
    return ""


# ---------------------------------------------------------------------------
# Class declaration
# ---------------------------------------------------------------------------

def _find_class_declaration(root: Node) -> Node | None:
    """
    Find the first top-level class or interface declaration.
    Repositories in Spring are typically interfaces extending JpaRepository.
    """
    for child in root.children:
        if child.type in ("class_declaration", "interface_declaration"):
            return child
    return None


def _get_class_name(class_node: Node, source: bytes) -> str:
    for child in class_node.children:
        if child.type == "identifier":
            return source[child.start_byte:child.end_byte].decode()
    return "Unknown"


def _get_class_annotations(class_node: Node, source: bytes) -> list[str]:
    annotations = []
    modifiers_node = None
    for child in class_node.children:
        if child.type == "modifiers":
            modifiers_node = child
            break
    if modifiers_node is None:
        return annotations
    for child in modifiers_node.children:
        if child.type in ("marker_annotation", "annotation"):
            name = _get_annotation_name(child, source)
            if name:
                annotations.append(name)
    return annotations


def _resolve_class_type(annotations: list[str]) -> ClassType:
    for ann in annotations:
        if ann in _CLASS_TYPE_MAP:
            return _CLASS_TYPE_MAP[ann]
    return ClassType.UNKNOWN


# ---------------------------------------------------------------------------
# Base URL from class-level @RequestMapping
# ---------------------------------------------------------------------------

def _get_base_url(class_node: Node, source: bytes) -> str:
    modifiers_node = None
    for child in class_node.children:
        if child.type == "modifiers":
            modifiers_node = child
            break
    if modifiers_node is None:
        return ""
    for child in modifiers_node.children:
        if child.type == "annotation":
            name = _get_annotation_name(child, source)
            if name == "RequestMapping":
                return _extract_annotation_value(child, source)
    return ""


# ---------------------------------------------------------------------------
# Class body members
# ---------------------------------------------------------------------------

def _get_class_body_members(class_node: Node):
    for child in class_node.children:
        if child.type == "class_body":
            for member in child.children:
                yield member


# ---------------------------------------------------------------------------
# Method helpers
# ---------------------------------------------------------------------------

def _get_node_annotations(method_node: Node, source: bytes) -> list[str]:
    annotations = []
    for child in method_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("marker_annotation", "annotation"):
                    name = _get_annotation_name(sub, source)
                    if name:
                        annotations.append(name)
    return annotations


def _get_identifier(node: Node, source: bytes) -> str:
    for child in node.children:
        if child.type == "identifier":
            return source[child.start_byte:child.end_byte].decode()
    return ""


def _get_return_type(method_node: Node, source: bytes) -> str:
    """Extract return type — handles generic types like ResponseEntity<Trade>."""
    for child in method_node.children:
        if child.type in (
            "type_identifier", "generic_type", "void_type",
            "integral_type", "boolean_type", "array_type"
        ):
            return source[child.start_byte:child.end_byte].decode()
    return "void"


def _get_parameter_types(method_node: Node, source: bytes) -> list[str]:
    """Extract parameter type names only (not variable names)."""
    params = []
    for child in method_node.children:
        if child.type == "formal_parameters":
            for param in child.children:
                if param.type == "formal_parameter":
                    for sub in param.children:
                        if sub.type in (
                            "type_identifier", "generic_type",
                            "integral_type", "boolean_type", "array_type"
                        ):
                            params.append(
                                source[sub.start_byte:sub.end_byte].decode()
                            )
                            break
    return params


# ---------------------------------------------------------------------------
# Endpoint resolution
# ---------------------------------------------------------------------------

def _resolve_endpoint(
    annotations: list[str],
    source: bytes,
    method_node: Node
) -> tuple[HttpMethod | None, str]:
    """
    Returns (HttpMethod, path) if this method is an endpoint, else (None, "").
    """
    for child in method_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("marker_annotation", "annotation"):
                    name = _get_annotation_name(sub, source)
                    if name in _HTTP_METHOD_MAP:
                        path = _extract_annotation_value(sub, source)
                        return _HTTP_METHOD_MAP[name], path
    return None, ""


# ---------------------------------------------------------------------------
# Dependency extraction (@Autowired fields)
# ---------------------------------------------------------------------------

def _extract_dependency(field_node: Node, source: bytes) -> DependencyInfo | None:
    """
    Extract @Autowired field as a DependencyInfo.
    Handles both @Autowired annotation and constructor injection detection.
    """
    is_autowired = False
    field_type   = ""
    field_name   = ""

    for child in field_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("marker_annotation", "annotation"):
                    name = _get_annotation_name(sub, source)
                    if name == "Autowired":
                        is_autowired = True

        elif child.type == "type_identifier":
            field_type = source[child.start_byte:child.end_byte].decode()

        elif child.type == "variable_declarator":
            for sub in child.children:
                if sub.type == "identifier":
                    field_name = source[sub.start_byte:sub.end_byte].decode()
                    break

    if is_autowired and field_type and field_name:
        return DependencyInfo(class_name=field_type, field_name=field_name)
    return None


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

def _get_annotation_name(annotation_node: Node, source: bytes) -> str:
    for child in annotation_node.children:
        if child.type == "identifier":
            return source[child.start_byte:child.end_byte].decode()
    return ""


def _extract_annotation_value(annotation_node: Node, source: bytes) -> str:
    """
    Extract the string value from an annotation.
    Handles: @PostMapping("/save") and @RequestMapping(value = "/save")
    """
    for child in annotation_node.children:
        if child.type == "annotation_argument_list":
            for sub in child.children:
                # Direct string: @PostMapping("/save")
                if sub.type == "string_literal":
                    raw = source[sub.start_byte:sub.end_byte].decode()
                    return raw.strip('"')
                # Named value: @RequestMapping(value = "/save")
                if sub.type == "element_value_pair":
                    for item in sub.children:
                        if item.type == "string_literal":
                            raw = source[item.start_byte:item.end_byte].decode()
                            return raw.strip('"')
    return ""


# ---------------------------------------------------------------------------
# Path joining
# ---------------------------------------------------------------------------

def _join_paths(base: str, path: str) -> str:
    """Combine base URL and method path cleanly."""
    base = base.rstrip("/")
    path = path.lstrip("/")
    if not base and not path:
        return "/"
    if not path:
        return base
    if not base:
        return "/" + path
    return f"{base}/{path}"
