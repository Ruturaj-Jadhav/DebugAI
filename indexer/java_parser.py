"""
java_parser.py — v3

Parses a single .java file using tree-sitter → ClassIndex.
This is the only file that knows about tree-sitter.

Fixes in v3:
  - Repository detection by supertype (extends JpaRepository/CrudRepository etc)
  - Test classes auto-typed as ClassType.TEST regardless of annotations
  - EndpointInfo stores base_path + relative_path + full_paths separately
  - handler_method_id added to EndpointInfo
  - MethodInfo.calls field removed
"""

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Node
from pathlib import Path

from .index_schema import (
    ClassIndex, ClassType, EndpointInfo, MethodInfo,
    DependencyInfo, HttpMethod, SCHEMA_VERSION
)

_JAVA_LANGUAGE = Language(tsjava.language())
_PARSER        = Parser(_JAVA_LANGUAGE)

# ---------------------------------------------------------------------------
# Spring Data supertypes that imply REPOSITORY even without @Repository
# ---------------------------------------------------------------------------
_REPOSITORY_SUPERTYPES = {
    "JpaRepository", "CrudRepository", "PagingAndSortingRepository",
    "MongoRepository", "ReactiveMongoRepository", "R2dbcRepository",
    "CoroutineCrudRepository", "Repository",
}

_CLASS_TYPE_MAP = {
    "RestController":        ClassType.CONTROLLER,
    "Controller":            ClassType.CONTROLLER,
    "Service":               ClassType.SERVICE,
    "Repository":            ClassType.REPOSITORY,
    "Component":             ClassType.COMPONENT,
    "Configuration":         ClassType.CONFIG,
    "SpringBootApplication": ClassType.CONFIG,
    "EnableWebMvc":          ClassType.CONFIG,
    "Entity":                ClassType.ENTITY,
    "MappedSuperclass":      ClassType.ENTITY,
    "Embeddable":            ClassType.ENTITY,
}

_HTTP_METHOD_MAP = {
    "GetMapping":     HttpMethod.GET,
    "PostMapping":    HttpMethod.POST,
    "PutMapping":     HttpMethod.PUT,
    "DeleteMapping":  HttpMethod.DELETE,
    "PatchMapping":   HttpMethod.PATCH,
    "RequestMapping": HttpMethod.ANY,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_source_set(file_path: str) -> str:
    parts = Path(file_path).parts
    if "test" in parts:
        return "test"
    if "main" in parts:
        return "main"
    return "other"


def _make_method_id(name: str, params: list[str]) -> str:
    return f"{name}({','.join(params)})"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_java_file(file_path: str) -> ClassIndex | None:
    source     = Path(file_path).read_bytes()
    tree       = _PARSER.parse(source)
    root       = tree.root_node
    source_set = _detect_source_set(file_path)

    package    = _extract_package(root, source)
    class_node = _find_class_declaration(root)
    if class_node is None:
        return None

    class_name  = _get_class_name(class_node, source)
    annotations = _get_class_annotations(class_node, source)
    supertypes  = _get_supertypes(class_node, source)

    # Test classes are always TEST regardless of annotations
    if source_set == "test":
        class_type = ClassType.TEST
    else:
        class_type = _resolve_class_type(annotations, supertypes)

    base_url = _get_base_url(class_node, source)

    methods      = []
    endpoints    = []
    dependencies = []

    for member in _get_class_body_members(class_node):
        if member.type == "method_declaration":
            method_name = _get_identifier(member, source)
            line_number = member.start_point[0] + 1
            params      = _get_parameter_types(member, source)
            return_type = _get_return_type(member, source)
            method_id   = _make_method_id(method_name, params)

            http_method, relative_path, raw_paths = _resolve_endpoint(member, source)
            if http_method is not None:
                full_paths = [_join_paths(base_url, p) for p in raw_paths] if raw_paths else [base_url or "/"]
                endpoints.append(EndpointInfo(
                    http_method       = http_method,
                    base_path         = base_url,
                    relative_path     = relative_path,
                    full_paths        = full_paths,
                    handler_name      = method_name,
                    handler_method_id = method_id,
                    line_number       = line_number,
                    parameters        = params,
                ))
            else:
                methods.append(MethodInfo(
                    name        = method_name,
                    method_id   = method_id,
                    line_number = line_number,
                    return_type = return_type,
                    parameters  = params,
                ))

        elif member.type == "constructor_declaration":
            constructor_deps = _extract_constructor_dependencies(member, source)
            dependencies.extend(constructor_deps)

        elif member.type == "field_declaration":
            dep = _extract_dependency(member, source)
            if dep:
                dependencies.append(dep)

    return ClassIndex(
        class_name   = class_name,
        file_path    = str(Path(file_path).resolve()),
        class_type   = class_type,
        package      = package,
        source_set   = source_set,
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
# Class / interface declaration
# ---------------------------------------------------------------------------

def _find_class_declaration(root: Node) -> Node | None:
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
    for child in class_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("marker_annotation", "annotation"):
                    name = _get_annotation_name(sub, source)
                    if name:
                        annotations.append(name)
    return annotations


def _get_supertypes(class_node: Node, source: bytes) -> list[str]:
    """
    Extract names from 'extends' and 'implements' clauses.
    Used to detect repositories that extend JpaRepository without @Repository.

    Handles generics: JpaRepository<Owner, Integer> → extracts "JpaRepository"
    """
    supertypes = []
    for child in class_node.children:
        # extends clause (class) or superclass (interface)
        if child.type in ("superclass", "super_interfaces", "extends_interfaces"):
            for sub in _walk(child):
                if sub.type == "type_identifier":
                    supertypes.append(source[sub.start_byte:sub.end_byte].decode())
    return supertypes


def _walk(node: Node):
    """Flat generator over all descendant nodes."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _resolve_class_type(annotations: list[str], supertypes: list[str]) -> ClassType:
    # Annotation-based detection first
    for ann in annotations:
        if ann in _CLASS_TYPE_MAP:
            return _CLASS_TYPE_MAP[ann]
    # Supertype-based fallback — catches unannotated Spring Data repositories
    for sup in supertypes:
        # strip generic: "JpaRepository<Owner,Integer>" → "JpaRepository"
        base = sup.split("<")[0].strip()
        if base in _REPOSITORY_SUPERTYPES:
            return ClassType.REPOSITORY
    return ClassType.UNKNOWN


def _get_base_url(class_node: Node, source: bytes) -> str:
    for child in class_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type == "annotation":
                    if _get_annotation_name(sub, source) == "RequestMapping":
                        paths = _extract_annotation_paths(sub, source)
                        return paths[0] if paths else ""
    return ""


# ---------------------------------------------------------------------------
# Class body
# ---------------------------------------------------------------------------

def _get_class_body_members(class_node: Node):
    for child in class_node.children:
        if child.type in ("class_body", "interface_body"):
            for member in child.children:
                yield member


# ---------------------------------------------------------------------------
# Method helpers
# ---------------------------------------------------------------------------

def _get_identifier(node: Node, source: bytes) -> str:
    for child in node.children:
        if child.type == "identifier":
            return source[child.start_byte:child.end_byte].decode()
    return ""


def _get_return_type(method_node: Node, source: bytes) -> str:
    for child in method_node.children:
        if child.type in (
            "type_identifier", "generic_type", "void_type",
            "integral_type", "boolean_type", "array_type"
        ):
            return source[child.start_byte:child.end_byte].decode()
    return "void"


def _get_parameter_types(method_node: Node, source: bytes) -> list[str]:
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
                            params.append(source[sub.start_byte:sub.end_byte].decode())
                            break
    return params


# ---------------------------------------------------------------------------
# Endpoint resolution — returns (HttpMethod, relative_path, all_raw_paths)
# ---------------------------------------------------------------------------

def _resolve_endpoint(
    method_node: Node,
    source: bytes,
) -> tuple[HttpMethod | None, str, list[str]]:
    """
    Returns (HttpMethod, first_relative_path, all_raw_paths) if endpoint.
    Returns (None, "", []) otherwise.
    """
    for child in method_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("marker_annotation", "annotation"):
                    name = _get_annotation_name(sub, source)
                    if name in _HTTP_METHOD_MAP:
                        raw_paths = _extract_annotation_paths(sub, source)
                        relative  = raw_paths[0] if raw_paths else ""
                        return _HTTP_METHOD_MAP[name], relative, raw_paths
    return None, "", []


def _extract_annotation_paths(annotation_node: Node, source: bytes) -> list[str]:
    """
    Extract path strings from a mapping annotation.
    Handles single, array, and named-value forms.
    """
    paths = []
    for child in annotation_node.children:
        if child.type == "annotation_argument_list":
            for sub in child.children:
                if sub.type == "string_literal":
                    paths.append(source[sub.start_byte:sub.end_byte].decode().strip('"'))
                elif sub.type == "array_initializer":
                    for item in sub.children:
                        if item.type == "string_literal":
                            paths.append(source[item.start_byte:item.end_byte].decode().strip('"'))
                elif sub.type == "element_value_pair":
                    for item in sub.children:
                        if item.type == "string_literal":
                            paths.append(source[item.start_byte:item.end_byte].decode().strip('"'))
                        elif item.type == "array_initializer":
                            for arr_item in item.children:
                                if arr_item.type == "string_literal":
                                    paths.append(source[arr_item.start_byte:arr_item.end_byte].decode().strip('"'))
    return paths


# ---------------------------------------------------------------------------
# Dependency extraction
# ---------------------------------------------------------------------------

def _extract_dependency(field_node: Node, source: bytes) -> DependencyInfo | None:
    is_autowired = False
    field_type   = ""
    field_name   = ""

    for child in field_node.children:
        if child.type == "modifiers":
            for sub in child.children:
                if sub.type in ("marker_annotation", "annotation"):
                    if _get_annotation_name(sub, source) == "Autowired":
                        is_autowired = True
        elif child.type == "type_identifier":
            field_type = source[child.start_byte:child.end_byte].decode()
        elif child.type == "variable_declarator":
            for sub in child.children:
                if sub.type == "identifier":
                    field_name = source[sub.start_byte:sub.end_byte].decode()
                    break

    if is_autowired and field_type and field_name:
        return DependencyInfo(class_name=field_type, field_name=field_name, dependency_kind="autowired")
    return None


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

def _get_annotation_name(annotation_node: Node, source: bytes) -> str:
    for child in annotation_node.children:
        if child.type == "identifier":
            return source[child.start_byte:child.end_byte].decode()
    return ""


def _join_paths(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = path.lstrip("/") if path else ""
    if not base and not path:
        return "/"
    if not path:
        return base
    if not base:
        return "/" + path
    return f"{base}/{path}"


# ---------------------------------------------------------------------------
# Constructor injection
# ---------------------------------------------------------------------------

def _extract_constructor_dependencies(constructor_node: Node, source: bytes) -> list[DependencyInfo]:
    """
    Extract constructor parameters as DependencyInfo entries.

    Modern Spring style — no @Autowired annotation needed:
        public OwnerController(OwnerRepository owners, VisitRepository visits) { ... }

    Returns one DependencyInfo per parameter.
    dependency_kind is "constructor" to distinguish from @Autowired field injection.

    Note: we extract ALL param types here. The index_builder is responsible for
    filtering to only classes that actually exist in the repo — that way we don't
    fabricate dependencies on String, int, or external framework types.
    """
    deps = []
    for child in constructor_node.children:
        if child.type == "formal_parameters":
            for param in child.children:
                if param.type == "formal_parameter":
                    param_type = ""
                    param_name = ""
                    for sub in param.children:
                        if sub.type == "type_identifier":
                            param_type = source[sub.start_byte:sub.end_byte].decode()
                        elif sub.type == "variable_declarator" or sub.type == "identifier":
                            # identifier is the param variable name
                            if sub.type == "identifier":
                                param_name = source[sub.start_byte:sub.end_byte].decode()
                    if param_type:
                        deps.append(DependencyInfo(
                            class_name      = param_type,
                            field_name      = param_name,   # variable name in constructor
                            dependency_kind = "constructor",
                        ))
    return deps