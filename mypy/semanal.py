"""The semantic analyzer.

Bind names to definitions and do various other simple consistency
checks.  Populate symbol tables.  The semantic analyzer also detects
special forms which reuse generic syntax such as NamedTuple and
cast().  Multiple analysis iterations may be needed to analyze forward
references and import cycles. Each iteration "fills in" additional
bindings and references until everything has been bound.

For example, consider this program:

  x = 1
  y = x

Here semantic analysis would detect that the assignment 'x = 1'
defines a new variable, the type of which is to be inferred (in a
later pass; type inference or type checking is not part of semantic
analysis).  Also, it would bind both references to 'x' to the same
module-level variable (Var) node.  The second assignment would also
be analyzed, and the type of 'y' marked as being inferred.

Semantic analysis of types is implemented in typeanal.py.

See semanal_main.py for the top-level logic.

Some important properties:

* After semantic analysis is complete, no PlaceholderNode and
  PlaceholderType instances should remain. During semantic analysis,
  if we encounter one of these, the current target should be deferred.

* A TypeInfo is only created once we know certain basic information about
  a type, such as the MRO, existence of a Tuple base class (e.g., for named
  tuples), and whether we have a TypedDict. We use a temporary
  PlaceholderNode node in the symbol table if some such information is
  missing.

* For assignments, we only add a non-placeholder symbol table entry once
  we know the sort of thing being defined (variable, NamedTuple, type alias,
  etc.).

* Every part of the analysis step must support multiple iterations over
  the same AST nodes, and each iteration must be able to fill in arbitrary
  things that were missing or incomplete in previous iterations.

* Changes performed by the analysis need to be reversible, since mypy
  daemon strips and reuses existing ASTs (to improve performance and/or
  reduce memory use).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Callable, Final, TypeVar, cast
from typing_extensions import TypeAlias as _TypeAlias, TypeGuard

from mypy import errorcodes as codes, message_registry
from mypy.constant_fold import constant_fold_expr
from mypy.errorcodes import PROPERTY_DECORATOR, ErrorCode
from mypy.errors import Errors, report_internal_error
from mypy.exprtotype import TypeTranslationError, expr_to_unanalyzed_type
from mypy.message_registry import ErrorMessage
from mypy.messages import (
    SUGGESTED_TEST_FIXTURES,
    TYPES_FOR_UNIMPORTED_HINTS,
    MessageBuilder,
    best_matches,
    pretty_seq,
)
from mypy.mro import MroError, calculate_mro
from mypy.nodes import (
    ARG_NAMED,
    ARG_POS,
    ARG_STAR2,
    CONTRAVARIANT,
    COVARIANT,
    GDEF,
    IMPLICITLY_ABSTRACT,
    INVARIANT,
    IS_ABSTRACT,
    LDEF,
    MDEF,
    NOT_ABSTRACT,
    PARAM_SPEC_KIND,
    REVEAL_LOCALS,
    REVEAL_TYPE,
    RUNTIME_PROTOCOL_DECOS,
    SYMBOL_FUNCBASE_TYPES,
    TYPE_VAR_KIND,
    TYPE_VAR_TUPLE_KIND,
    VARIANCE_NOT_READY,
    ArgKind,
    AssertStmt,
    AssertTypeExpr,
    AssignmentExpr,
    AssignmentStmt,
    AwaitExpr,
    Block,
    BreakStmt,
    BytesExpr,
    CallExpr,
    CastExpr,
    ClassDef,
    ComparisonExpr,
    ComplexExpr,
    ConditionalExpr,
    Context,
    ContinueStmt,
    DataclassTransformSpec,
    Decorator,
    DelStmt,
    DictExpr,
    DictionaryComprehension,
    EllipsisExpr,
    EnumCallExpr,
    Expression,
    ExpressionStmt,
    FakeExpression,
    FloatExpr,
    ForStmt,
    FuncBase,
    FuncDef,
    FuncItem,
    GeneratorExpr,
    GlobalDecl,
    IfStmt,
    Import,
    ImportAll,
    ImportBase,
    ImportFrom,
    IndexExpr,
    IntExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    Lvalue,
    MatchStmt,
    MemberExpr,
    MypyFile,
    NamedTupleExpr,
    NameExpr,
    Node,
    NonlocalDecl,
    OperatorAssignmentStmt,
    OpExpr,
    OverloadedFuncDef,
    OverloadPart,
    ParamSpecExpr,
    PassStmt,
    PlaceholderNode,
    PromoteExpr,
    RaiseStmt,
    RefExpr,
    ReturnStmt,
    RevealExpr,
    SetComprehension,
    SetExpr,
    SliceExpr,
    StarExpr,
    Statement,
    StrExpr,
    SuperExpr,
    SymbolNode,
    SymbolTable,
    SymbolTableNode,
    TempNode,
    TryStmt,
    TupleExpr,
    TypeAlias,
    TypeAliasExpr,
    TypeAliasStmt,
    TypeApplication,
    TypedDictExpr,
    TypeInfo,
    TypeParam,
    TypeVarExpr,
    TypeVarLikeExpr,
    TypeVarTupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
    YieldExpr,
    YieldFromExpr,
    get_member_expr_fullname,
    implicit_module_attrs,
    is_final_node,
    type_aliases,
    type_aliases_source_versions,
    typing_extensions_aliases,
)
from mypy.options import Options
from mypy.patterns import (
    AsPattern,
    ClassPattern,
    MappingPattern,
    OrPattern,
    SequencePattern,
    SingletonPattern,
    StarredPattern,
    ValuePattern,
)
from mypy.plugin import (
    ClassDefContext,
    DynamicClassDefContext,
    Plugin,
    SemanticAnalyzerPluginInterface,
)
from mypy.plugins import dataclasses as dataclasses_plugin
from mypy.reachability import (
    ALWAYS_FALSE,
    ALWAYS_TRUE,
    MYPY_FALSE,
    MYPY_TRUE,
    infer_condition_value,
    infer_reachability_of_if_statement,
    infer_reachability_of_match_statement,
)
from mypy.scope import Scope
from mypy.semanal_enum import EnumCallAnalyzer
from mypy.semanal_namedtuple import NamedTupleAnalyzer
from mypy.semanal_newtype import NewTypeAnalyzer
from mypy.semanal_shared import (
    ALLOW_INCOMPATIBLE_OVERRIDE,
    PRIORITY_FALLBACKS,
    SemanticAnalyzerInterface,
    calculate_tuple_fallback,
    find_dataclass_transform_spec,
    has_placeholder,
    parse_bool,
    require_bool_literal_argument,
    set_callable_name as set_callable_name,
)
from mypy.semanal_typeddict import TypedDictAnalyzer
from mypy.tvar_scope import TypeVarLikeScope
from mypy.typeanal import (
    SELF_TYPE_NAMES,
    FindTypeVarVisitor,
    TypeAnalyser,
    TypeVarDefaultTranslator,
    TypeVarLikeList,
    analyze_type_alias,
    check_for_explicit_any,
    detect_diverging_alias,
    find_self_type,
    fix_instance,
    has_any_from_unimported_type,
    type_constructors,
    validate_instance,
)
from mypy.typeops import function_type, get_type_vars, try_getting_str_literals_from_type
from mypy.types import (
    ASSERT_TYPE_NAMES,
    DATACLASS_TRANSFORM_NAMES,
    DEPRECATED_TYPE_NAMES,
    FINAL_DECORATOR_NAMES,
    FINAL_TYPE_NAMES,
    IMPORTED_REVEAL_TYPE_NAMES,
    NEVER_NAMES,
    OVERLOAD_NAMES,
    OVERRIDE_DECORATOR_NAMES,
    PROTOCOL_NAMES,
    REVEAL_TYPE_NAMES,
    TPDICT_NAMES,
    TYPE_ALIAS_NAMES,
    TYPE_CHECK_ONLY_NAMES,
    TYPE_NAMES,
    TYPE_VAR_LIKE_NAMES,
    TYPED_NAMEDTUPLE_NAMES,
    UNPACK_TYPE_NAMES,
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    Overloaded,
    Parameters,
    ParamSpecType,
    PlaceholderType,
    ProperType,
    TrivialSyntheticTypeTranslator,
    TupleType,
    Type,
    TypeAliasType,
    TypedDictType,
    TypeOfAny,
    TypeType,
    TypeVarId,
    TypeVarLikeType,
    TypeVarTupleType,
    TypeVarType,
    UnboundType,
    UnionType,
    UnpackType,
    get_proper_type,
    get_proper_types,
    has_type_vars,
    is_named_instance,
    remove_dups,
    type_vars_as_args,
)
from mypy.types_utils import is_invalid_recursive_alias, store_argument_type
from mypy.typevars import fill_typevars
from mypy.util import correct_relative_import, is_dunder, module_prefix, unmangle, unnamed_function
from mypy.visitor import NodeVisitor

T = TypeVar("T")


FUTURE_IMPORTS: Final = {
    "__future__.nested_scopes": "nested_scopes",
    "__future__.generators": "generators",
    "__future__.division": "division",
    "__future__.absolute_import": "absolute_import",
    "__future__.with_statement": "with_statement",
    "__future__.print_function": "print_function",
    "__future__.unicode_literals": "unicode_literals",
    "__future__.barry_as_FLUFL": "barry_as_FLUFL",
    "__future__.generator_stop": "generator_stop",
    "__future__.annotations": "annotations",
}


# Special cased built-in classes that are needed for basic functionality and need to be
# available very early on.
CORE_BUILTIN_CLASSES: Final = ["object", "bool", "function"]


# Python has several different scope/namespace kinds with subtly different semantics.
SCOPE_GLOBAL: Final = 0  # Module top level
SCOPE_CLASS: Final = 1  # Class body
SCOPE_FUNC: Final = 2  # Function or lambda
SCOPE_COMPREHENSION: Final = 3  # Comprehension or generator expression
SCOPE_ANNOTATION: Final = 4  # Annotation scopes for type parameters and aliases (PEP 695)


# Used for tracking incomplete references
Tag: _TypeAlias = int


class SemanticAnalyzer(
    NodeVisitor[None], SemanticAnalyzerInterface, SemanticAnalyzerPluginInterface
):
    """Semantically analyze parsed mypy files.

    The analyzer binds names and does various consistency checks for an
    AST. Note that type checking is performed as a separate pass.
    """

    __deletable__ = ["patches", "options", "cur_mod_node"]

    # Module name space
    modules: dict[str, MypyFile]
    # Global name space for current module
    globals: SymbolTable
    # Names declared using "global" (separate set for each scope)
    global_decls: list[set[str]]
    # Names declared using "nonlocal" (separate set for each scope)
    nonlocal_decls: list[set[str]]
    # Local names of function scopes; None for non-function scopes.
    locals: list[SymbolTable | None]
    # Type of each scope (SCOPE_*, indexes match locals)
    scope_stack: list[int]
    # Nested block depths of scopes
    block_depth: list[int]
    # TypeInfo of directly enclosing class (or None)
    _type: TypeInfo | None = None
    # Stack of outer classes (the second tuple item contains tvars).
    type_stack: list[TypeInfo | None]
    # Type variables bound by the current scope, be it class or function
    tvar_scope: TypeVarLikeScope
    # Per-module options
    options: Options

    # Stack of functions being analyzed
    function_stack: list[FuncItem]

    # Set to True if semantic analysis defines a name, or replaces a
    # placeholder definition. If some iteration makes no progress,
    # there can be at most one additional final iteration (see below).
    progress = False
    deferred = False  # Set to true if another analysis pass is needed
    incomplete = False  # Set to true if current module namespace is missing things
    # Is this the final iteration of semantic analysis (where we report
    # unbound names due to cyclic definitions and should not defer)?
    _final_iteration = False
    # These names couldn't be added to the symbol table due to incomplete deps.
    # Note that missing names are per module, _not_ per namespace. This means that e.g.
    # a missing name at global scope will block adding same name at a class scope.
    # This should not affect correctness and is purely a performance issue,
    # since it can cause unnecessary deferrals. These are represented as
    # PlaceholderNodes in the symbol table. We use this to ensure that the first
    # definition takes precedence even if it's incomplete.
    #
    # Note that a star import adds a special name '*' to the set, this blocks
    # adding _any_ names in the current file.
    missing_names: list[set[str]]
    # Callbacks that will be called after semantic analysis to tweak things.
    patches: list[tuple[int, Callable[[], None]]]
    loop_depth: list[int]  # Depth of breakable loops
    cur_mod_id = ""  # Current module id (or None) (phase 2)
    _is_stub_file = False  # Are we analyzing a stub file?
    _is_typeshed_stub_file = False  # Are we analyzing a typeshed stub file?
    imports: set[str]  # Imported modules (during phase 2 analysis)
    # Note: some imports (and therefore dependencies) might
    # not be found in phase 1, for example due to * imports.
    errors: Errors  # Keeps track of generated errors
    plugin: Plugin  # Mypy plugin for special casing of library features
    statement: Statement | None = None  # Statement/definition being analyzed

    # Mapping from 'async def' function definitions to their return type wrapped as a
    # 'Coroutine[Any, Any, T]'. Used to keep track of whether a function definition's
    # return type has already been wrapped, by checking if the function definition's
    # type is stored in this mapping and that it still matches.
    wrapped_coro_return_types: dict[FuncDef, Type] = {}

    def __init__(
        self,
        modules: dict[str, MypyFile],
        missing_modules: set[str],
        incomplete_namespaces: set[str],
        errors: Errors,
        plugin: Plugin,
    ) -> None:
        """Construct semantic analyzer.

        We reuse the same semantic analyzer instance across multiple modules.

        Args:
            modules: Global modules dictionary
            missing_modules: Modules that could not be imported encountered so far
            incomplete_namespaces: Namespaces that are being populated during semantic analysis
                (can contain modules and classes within the current SCC; mutated by the caller)
            errors: Report analysis errors using this instance
        """
        self.locals = [None]
        self.scope_stack = [SCOPE_GLOBAL]
        # Saved namespaces from previous iteration. Every top-level function/method body is
        # analyzed in several iterations until all names are resolved. We need to save
        # the local namespaces for the top level function and all nested functions between
        # these iterations. See also semanal_main.process_top_level_function().
        self.saved_locals: dict[
            FuncItem | GeneratorExpr | DictionaryComprehension, SymbolTable
        ] = {}
        self.imports = set()
        self._type = None
        self.type_stack = []
        # Are the namespaces of classes being processed complete?
        self.incomplete_type_stack: list[bool] = []
        self.tvar_scope = TypeVarLikeScope()
        self.function_stack = []
        self.block_depth = [0]
        self.loop_depth = [0]
        self.errors = errors
        self.modules = modules
        self.msg = MessageBuilder(errors, modules)
        self.missing_modules = missing_modules
        self.missing_names = [set()]
        # These namespaces are still in process of being populated. If we encounter a
        # missing name in these namespaces, we need to defer the current analysis target,
        # since it's possible that the name will be there once the namespace is complete.
        self.incomplete_namespaces = incomplete_namespaces
        self.all_exports: list[str] = []
        # Map from module id to list of explicitly exported names (i.e. names in __all__).
        self.export_map: dict[str, list[str]] = {}
        self.plugin = plugin
        # If True, process function definitions. If False, don't. This is used
        # for processing module top levels in fine-grained incremental mode.
        self.recurse_into_functions = True
        self.scope = Scope()

        # Trace line numbers for every file where deferral happened during analysis of
        # current SCC or top-level function.
        self.deferral_debug_context: list[tuple[str, int]] = []

        # This is needed to properly support recursive type aliases. The problem is that
        # Foo[Bar] could mean three things depending on context: a target for type alias,
        # a normal index expression (including enum index), or a type application.
        # The latter is particularly problematic as it can falsely create incomplete
        # refs while analysing rvalues of type aliases. To avoid this we first analyse
        # rvalues while temporarily setting this to True.
        self.basic_type_applications = False

        # Used to temporarily enable unbound type variables in some contexts. Namely,
        # in base class expressions, and in right hand sides of type aliases. Do not add
        # new uses of this, as this may cause leaking `UnboundType`s to type checking.
        self.allow_unbound_tvars = False

        # Used to pass information about current overload index to visit_func_def().
        self.current_overload_item: int | None = None

        # Used to track whether currently inside an except* block. This helps
        # to invoke errors when continue/break/return is used inside except* block.
        self.inside_except_star_block: bool = False
        # Used to track edge case when return is still inside except* if it enters a loop
        self.return_stmt_inside_except_star_block: bool = False

    # mypyc doesn't properly handle implementing an abstractproperty
    # with a regular attribute so we make them properties
    @property
    def type(self) -> TypeInfo | None:
        return self._type

    @property
    def is_stub_file(self) -> bool:
        return self._is_stub_file

    @property
    def is_typeshed_stub_file(self) -> bool:
        return self._is_typeshed_stub_file

    @property
    def final_iteration(self) -> bool:
        return self._final_iteration

    @contextmanager
    def allow_unbound_tvars_set(self) -> Iterator[None]:
        old = self.allow_unbound_tvars
        self.allow_unbound_tvars = True
        try:
            yield
        finally:
            self.allow_unbound_tvars = old

    @contextmanager
    def inside_except_star_block_set(
        self, value: bool, entering_loop: bool = False
    ) -> Iterator[None]:
        old = self.inside_except_star_block
        self.inside_except_star_block = value

        # Return statement would still be in except* scope if entering loops
        if not entering_loop:
            old_return_stmt_flag = self.return_stmt_inside_except_star_block
            self.return_stmt_inside_except_star_block = value

        try:
            yield
        finally:
            self.inside_except_star_block = old
            if not entering_loop:
                self.return_stmt_inside_except_star_block = old_return_stmt_flag

    #
    # Preparing module (performed before semantic analysis)
    #

    def prepare_file(self, file_node: MypyFile) -> None:
        """Prepare a freshly parsed file for semantic analysis."""
        if "builtins" in self.modules:
            file_node.names["__builtins__"] = SymbolTableNode(GDEF, self.modules["builtins"])
        if file_node.fullname == "builtins":
            self.prepare_builtins_namespace(file_node)
        if file_node.fullname == "typing":
            self.prepare_typing_namespace(file_node, type_aliases)
        if file_node.fullname == "typing_extensions":
            self.prepare_typing_namespace(file_node, typing_extensions_aliases)

    def prepare_typing_namespace(self, file_node: MypyFile, aliases: dict[str, str]) -> None:
        """Remove dummy alias definitions such as List = TypeAlias(object) from typing.

        They will be replaced with real aliases when corresponding targets are ready.
        """

        # This is all pretty unfortunate. typeshed now has a
        # sys.version_info check for OrderedDict, and we shouldn't
        # take it out, because it is correct and a typechecker should
        # use that as a source of truth. But instead we rummage
        # through IfStmts to remove the info first.  (I tried to
        # remove this whole machinery and ran into issues with the
        # builtins/typing import cycle.)
        def helper(defs: list[Statement]) -> None:
            for stmt in defs.copy():
                if isinstance(stmt, IfStmt):
                    for body in stmt.body:
                        helper(body.body)
                    if stmt.else_body:
                        helper(stmt.else_body.body)
                if (
                    isinstance(stmt, AssignmentStmt)
                    and len(stmt.lvalues) == 1
                    and isinstance(stmt.lvalues[0], NameExpr)
                ):
                    # Assignment to a simple name, remove it if it is a dummy alias.
                    if f"{file_node.fullname}.{stmt.lvalues[0].name}" in aliases:
                        defs.remove(stmt)

        helper(file_node.defs)

    def prepare_builtins_namespace(self, file_node: MypyFile) -> None:
        """Add certain special-cased definitions to the builtins module.

        Some definitions are too special or fundamental to be processed
        normally from the AST.
        """
        names = file_node.names

        # Add empty definition for core built-in classes, since they are required for basic
        # operation. These will be completed later on.
        for name in CORE_BUILTIN_CLASSES:
            cdef = ClassDef(name, Block([]))  # Dummy ClassDef, will be replaced later
            info = TypeInfo(SymbolTable(), cdef, "builtins")
            info._fullname = f"builtins.{name}"
            names[name] = SymbolTableNode(GDEF, info)

        bool_info = names["bool"].node
        assert isinstance(bool_info, TypeInfo)
        bool_type = Instance(bool_info, [])

        special_var_types: list[tuple[str, Type]] = [
            ("None", NoneType()),
            # reveal_type is a mypy-only function that gives an error with
            # the type of its arg.
            ("reveal_type", AnyType(TypeOfAny.special_form)),
            # reveal_locals is a mypy-only function that gives an error with the types of
            # locals
            ("reveal_locals", AnyType(TypeOfAny.special_form)),
            ("True", bool_type),
            ("False", bool_type),
            ("__debug__", bool_type),
        ]

        for name, typ in special_var_types:
            v = Var(name, typ)
            v._fullname = f"builtins.{name}"
            file_node.names[name] = SymbolTableNode(GDEF, v)

    #
    # Analyzing a target
    #

    def refresh_partial(
        self,
        node: MypyFile | FuncDef | OverloadedFuncDef,
        patches: list[tuple[int, Callable[[], None]]],
        final_iteration: bool,
        file_node: MypyFile,
        options: Options,
        active_type: TypeInfo | None = None,
    ) -> None:
        """Refresh a stale target in fine-grained incremental mode."""
        self.patches = patches
        self.deferred = False
        self.incomplete = False
        self._final_iteration = final_iteration
        self.missing_names[-1] = set()

        with self.file_context(file_node, options, active_type):
            if isinstance(node, MypyFile):
                self.refresh_top_level(node)
            else:
                self.recurse_into_functions = True
                self.accept(node)
        del self.patches

    def refresh_top_level(self, file_node: MypyFile) -> None:
        """Reanalyze a stale module top-level in fine-grained incremental mode."""
        if self.options.allow_redefinition_new and not self.options.local_partial_types:
            n = TempNode(AnyType(TypeOfAny.special_form))
            n.line = 1
            n.column = 0
            n.end_line = 1
            n.end_column = 0
            self.fail("--local-partial-types must be enabled if using --allow-redefinition-new", n)
        self.recurse_into_functions = False
        self.add_implicit_module_attrs(file_node)
        for d in file_node.defs:
            self.accept(d)
        if file_node.fullname == "typing":
            self.add_builtin_aliases(file_node)
        if file_node.fullname == "typing_extensions":
            self.add_typing_extension_aliases(file_node)
        self.adjust_public_exports()
        self.export_map[self.cur_mod_id] = self.all_exports
        self.all_exports = []

    def add_implicit_module_attrs(self, file_node: MypyFile) -> None:
        """Manually add implicit definitions of module '__name__' etc."""
        str_type: Type | None = self.named_type_or_none("builtins.str")
        if str_type is None:
            str_type = UnboundType("builtins.str")
        inst: Type | None
        for name, t in implicit_module_attrs.items():
            if name == "__doc__":
                typ: Type = str_type
            elif name == "__path__":
                if not file_node.is_package_init_file():
                    continue
                # Need to construct the type ourselves, to avoid issues with __builtins__.list
                # not being subscriptable or typing.List not getting bound
                inst = self.named_type_or_none("builtins.list", [str_type])
                if inst is None:
                    assert not self.final_iteration, "Cannot find builtins.list to add __path__"
                    self.defer()
                    return
                typ = inst
            elif name == "__annotations__":
                inst = self.named_type_or_none(
                    "builtins.dict", [str_type, AnyType(TypeOfAny.special_form)]
                )
                if inst is None:
                    assert (
                        not self.final_iteration
                    ), "Cannot find builtins.dict to add __annotations__"
                    self.defer()
                    return
                typ = inst
            elif name == "__spec__":
                if self.options.use_builtins_fixtures:
                    inst = self.named_type_or_none("builtins.object")
                else:
                    inst = self.named_type_or_none("importlib.machinery.ModuleSpec")
                if inst is None:
                    if self.final_iteration:
                        inst = self.named_type_or_none("builtins.object")
                        assert inst is not None, "Cannot find builtins.object"
                    else:
                        self.defer()
                        return
                if file_node.name == "__main__":
                    # https://docs.python.org/3/reference/import.html#main-spec
                    inst = UnionType.make_union([inst, NoneType()])
                typ = inst
            else:
                assert t is not None, f"type should be specified for {name}"
                typ = UnboundType(t)

            existing = file_node.names.get(name)
            if existing is not None and not isinstance(existing.node, PlaceholderNode):
                # Already exists.
                continue

            an_type = self.anal_type(typ)
            if an_type:
                var = Var(name, an_type)
                var._fullname = self.qualified_name(name)
                var.is_ready = True
                self.add_symbol(name, var, dummy_context())
            else:
                self.add_symbol(
                    name,
                    PlaceholderNode(self.qualified_name(name), file_node, -1),
                    dummy_context(),
                )

    def add_builtin_aliases(self, tree: MypyFile) -> None:
        """Add builtin type aliases to typing module.

        For historical reasons, the aliases like `List = list` are not defined
        in typeshed stubs for typing module. Instead we need to manually add the
        corresponding nodes on the fly. We explicitly mark these aliases as normalized,
        so that a user can write `typing.List[int]`.
        """
        assert tree.fullname == "typing"
        for alias, target_name in type_aliases.items():
            if (
                alias in type_aliases_source_versions
                and type_aliases_source_versions[alias] > self.options.python_version
            ):
                # This alias is not available on this Python version.
                continue
            name = alias.split(".")[-1]
            if name in tree.names and not isinstance(tree.names[name].node, PlaceholderNode):
                continue
            self.create_alias(tree, target_name, alias, name)

    def add_typing_extension_aliases(self, tree: MypyFile) -> None:
        """Typing extensions module does contain some type aliases.

        We need to analyze them as such, because in typeshed
        they are just defined as `_Alias()` call.
        Which is not supported natively.
        """
        assert tree.fullname == "typing_extensions"

        for alias, target_name in typing_extensions_aliases.items():
            name = alias.split(".")[-1]
            if name in tree.names and isinstance(tree.names[name].node, TypeAlias):
                continue  # Do not reset TypeAliases on the second pass.

            # We need to remove any node that is there at the moment. It is invalid.
            tree.names.pop(name, None)

            # Now, create a new alias.
            self.create_alias(tree, target_name, alias, name)

    def create_alias(self, tree: MypyFile, target_name: str, alias: str, name: str) -> None:
        tag = self.track_incomplete_refs()
        n = self.lookup_fully_qualified_or_none(target_name)
        if n:
            if isinstance(n.node, PlaceholderNode):
                self.mark_incomplete(name, tree)
            else:
                # Found built-in class target. Create alias.
                target = self.named_type_or_none(target_name, [])
                assert target is not None
                # Transform List to List[Any], etc.
                fix_instance(
                    target, self.fail, self.note, disallow_any=False, options=self.options
                )
                alias_node = TypeAlias(
                    target,
                    alias,
                    line=-1,
                    column=-1,  # there is no context
                    no_args=True,
                    normalized=True,
                )
                self.add_symbol(name, alias_node, tree)
        elif self.found_incomplete_ref(tag):
            # Built-in class target may not ready yet -- defer.
            self.mark_incomplete(name, tree)
        else:
            # Test fixtures may be missing some builtin classes, which is okay.
            # Kill the placeholder if there is one.
            if name in tree.names:
                assert isinstance(tree.names[name].node, PlaceholderNode)
                del tree.names[name]

    def adjust_public_exports(self) -> None:
        """Adjust the module visibility of globals due to __all__."""
        if "__all__" in self.globals:
            for name, g in self.globals.items():
                # Being included in __all__ explicitly exports and makes public.
                if name in self.all_exports:
                    g.module_public = True
                    g.module_hidden = False
                # But when __all__ is defined, and a symbol is not included in it,
                # it cannot be public.
                else:
                    g.module_public = False

    @contextmanager
    def file_context(
        self, file_node: MypyFile, options: Options, active_type: TypeInfo | None = None
    ) -> Iterator[None]:
        """Configure analyzer for analyzing targets within a file/class.

        Args:
            file_node: target file
            options: options specific to the file
            active_type: must be the surrounding class to analyze method targets
        """
        scope = self.scope
        self.options = options
        self.errors.set_file(file_node.path, file_node.fullname, scope=scope, options=options)
        self.cur_mod_node = file_node
        self.cur_mod_id = file_node.fullname
        with scope.module_scope(self.cur_mod_id):
            self._is_stub_file = file_node.path.lower().endswith(".pyi")
            self._is_typeshed_stub_file = file_node.is_typeshed_file(options)
            self.globals = file_node.names
            self.tvar_scope = TypeVarLikeScope()

            self.named_tuple_analyzer = NamedTupleAnalyzer(options, self, self.msg)
            self.typed_dict_analyzer = TypedDictAnalyzer(options, self, self.msg)
            self.enum_call_analyzer = EnumCallAnalyzer(options, self)
            self.newtype_analyzer = NewTypeAnalyzer(options, self, self.msg)

            # Counter that keeps track of references to undefined things potentially caused by
            # incomplete namespaces.
            self.num_incomplete_refs = 0

            if active_type:
                enclosing_fullname = active_type.fullname.rsplit(".", 1)[0]
                if "." in enclosing_fullname:
                    enclosing_node = self.lookup_fully_qualified_or_none(enclosing_fullname)
                    if enclosing_node and isinstance(enclosing_node.node, TypeInfo):
                        self._type = enclosing_node.node
                self.push_type_args(active_type.defn.type_args, active_type.defn)
                self.incomplete_type_stack.append(False)
                scope.enter_class(active_type)
                self.enter_class(active_type.defn.info)
                for tvar in active_type.defn.type_vars:
                    self.tvar_scope.bind_existing(tvar)

            yield

            if active_type:
                scope.leave_class()
                self.leave_class()
                self._type = None
                self.incomplete_type_stack.pop()
                self.pop_type_args(active_type.defn.type_args)
        del self.options

    #
    # Functions
    #

    def visit_func_def(self, defn: FuncDef) -> None:
        self.statement = defn

        # Visit default values because they may contain assignment expressions.
        for arg in defn.arguments:
            if arg.initializer:
                arg.initializer.accept(self)

        defn.is_conditional = self.block_depth[-1] > 0

        # Set full names even for those definitions that aren't added
        # to a symbol table. For example, for overload items.
        defn._fullname = self.qualified_name(defn.name)

        # We don't add module top-level functions to symbol tables
        # when we analyze their bodies in the second phase on analysis,
        # since they were added in the first phase. Nested functions
        # get always added, since they aren't separate targets.
        if not self.recurse_into_functions or len(self.function_stack) > 0:
            if not defn.is_decorated and not defn.is_overload:
                self.add_function_to_symbol_table(defn)

        if not self.recurse_into_functions:
            return

        with self.scope.function_scope(defn):
            with self.inside_except_star_block_set(value=False):
                self.analyze_func_def(defn)

    def function_fullname(self, fullname: str) -> str:
        if self.current_overload_item is None:
            return fullname
        return f"{fullname}#{self.current_overload_item}"

    def analyze_func_def(self, defn: FuncDef) -> None:
        if self.push_type_args(defn.type_args, defn) is None:
            self.defer(defn)
            return

        self.function_stack.append(defn)

        if defn.type:
            assert isinstance(defn.type, CallableType)
            has_self_type = self.update_function_type_variables(defn.type, defn)
        else:
            has_self_type = False

        self.function_stack.pop()

        if self.is_class_scope():
            # Method definition
            assert self.type is not None
            defn.info = self.type
            if defn.type is not None and defn.name in ("__init__", "__init_subclass__"):
                assert isinstance(defn.type, CallableType)
                if isinstance(get_proper_type(defn.type.ret_type), AnyType):
                    defn.type = defn.type.copy_modified(ret_type=NoneType())
            self.prepare_method_signature(defn, self.type, has_self_type)

        # Analyze function signature
        fullname = self.function_fullname(defn.fullname)
        with self.tvar_scope_frame(self.tvar_scope.method_frame(fullname)):
            if defn.type:
                self.check_classvar_in_signature(defn.type)
                assert isinstance(defn.type, CallableType)
                # Signature must be analyzed in the surrounding scope so that
                # class-level imported names and type variables are in scope.
                analyzer = self.type_analyzer()
                tag = self.track_incomplete_refs()
                result = analyzer.visit_callable_type(defn.type, nested=False, namespace=fullname)
                # Don't store not ready types (including placeholders).
                if self.found_incomplete_ref(tag) or has_placeholder(result):
                    self.defer(defn)
                    self.pop_type_args(defn.type_args)
                    return
                assert isinstance(result, ProperType)
                if isinstance(result, CallableType):
                    # type guards need to have a positional argument, to spec
                    skip_self = self.is_class_scope() and not defn.is_static
                    if result.type_guard and ARG_POS not in result.arg_kinds[skip_self:]:
                        self.fail(
                            "TypeGuard functions must have a positional argument",
                            result,
                            code=codes.VALID_TYPE,
                        )
                        # in this case, we just kind of just ... remove the type guard.
                        result = result.copy_modified(type_guard=None)
                    if result.type_is and ARG_POS not in result.arg_kinds[skip_self:]:
                        self.fail(
                            '"TypeIs" functions must have a positional argument',
                            result,
                            code=codes.VALID_TYPE,
                        )
                        result = result.copy_modified(type_is=None)

                    result = self.remove_unpack_kwargs(defn, result)
                    if has_self_type and self.type is not None:
                        info = self.type
                        if info.self_type is not None:
                            result.variables = [info.self_type] + list(result.variables)
                defn.type = result
                self.add_type_alias_deps(analyzer.aliases_used)
                self.check_function_signature(defn)
                if isinstance(defn, FuncDef):
                    assert isinstance(defn.type, CallableType)
                    defn.type = set_callable_name(defn.type, defn)

        self.analyze_arg_initializers(defn)
        self.analyze_function_body(defn)

        if self.is_class_scope():
            assert self.type is not None
            # Mark protocol methods with empty bodies as implicitly abstract.
            # This makes explicit protocol subclassing type-safe.
            if (
                self.type.is_protocol
                and not self.is_stub_file  # Bodies in stub files are always empty.
                and (not isinstance(self.scope.function, OverloadedFuncDef) or defn.is_property)
                and defn.abstract_status != IS_ABSTRACT
                and is_trivial_body(defn.body)
            ):
                defn.abstract_status = IMPLICITLY_ABSTRACT
            if (
                is_trivial_body(defn.body)
                and not self.is_stub_file
                and defn.abstract_status != NOT_ABSTRACT
            ):
                defn.is_trivial_body = True

        if (
            defn.is_coroutine
            and isinstance(defn.type, CallableType)
            and self.wrapped_coro_return_types.get(defn) != defn.type
        ):
            if defn.is_async_generator:
                # Async generator types are handled elsewhere
                pass
            else:
                # A coroutine defined as `async def foo(...) -> T: ...`
                # has external return type `Coroutine[Any, Any, T]`.
                any_type = AnyType(TypeOfAny.special_form)
                ret_type = self.named_type_or_none(
                    "typing.Coroutine", [any_type, any_type, defn.type.ret_type]
                )
                assert ret_type is not None, "Internal error: typing.Coroutine not found"
                defn.type = defn.type.copy_modified(ret_type=ret_type)
                self.wrapped_coro_return_types[defn] = defn.type

        self.pop_type_args(defn.type_args)

    def remove_unpack_kwargs(self, defn: FuncDef, typ: CallableType) -> CallableType:
        if not typ.arg_kinds or typ.arg_kinds[-1] is not ArgKind.ARG_STAR2:
            return typ
        last_type = typ.arg_types[-1]
        if not isinstance(last_type, UnpackType):
            return typ
        last_type = get_proper_type(last_type.type)
        if not isinstance(last_type, TypedDictType):
            self.fail("Unpack item in ** argument must be a TypedDict", last_type)
            new_arg_types = typ.arg_types[:-1] + [AnyType(TypeOfAny.from_error)]
            return typ.copy_modified(arg_types=new_arg_types)
        overlap = set(typ.arg_names) & set(last_type.items)
        # It is OK for TypedDict to have a key named 'kwargs'.
        overlap.discard(typ.arg_names[-1])
        if overlap:
            overlapped = ", ".join([f'"{name}"' for name in overlap])
            self.fail(f"Overlap between argument names and ** TypedDict items: {overlapped}", defn)
            new_arg_types = typ.arg_types[:-1] + [AnyType(TypeOfAny.from_error)]
            return typ.copy_modified(arg_types=new_arg_types)
        # OK, everything looks right now, mark the callable type as using unpack.
        new_arg_types = typ.arg_types[:-1] + [last_type]
        return typ.copy_modified(arg_types=new_arg_types, unpack_kwargs=True)

    def prepare_method_signature(self, func: FuncDef, info: TypeInfo, has_self_type: bool) -> None:
        """Check basic signature validity and tweak annotation of self/cls argument."""
        # Only non-static methods are special, as well as __new__.
        functype = func.type
        if func.name == "__new__":
            func.is_static = True
        if func.has_self_or_cls_argument:
            if func.name in ["__init_subclass__", "__class_getitem__"]:
                func.is_class = True
            if not func.arguments:
                self.fail(
                    'Method must have at least one argument. Did you forget the "self" argument?',
                    func,
                )
            elif isinstance(functype, CallableType):
                self_type = get_proper_type(functype.arg_types[0])
                if isinstance(self_type, AnyType):
                    if has_self_type:
                        assert self.type is not None and self.type.self_type is not None
                        leading_type: Type = self.type.self_type
                    else:
                        func.is_trivial_self = True
                        leading_type = fill_typevars(info)
                    if func.is_class or func.name == "__new__":
                        leading_type = self.class_type(leading_type)
                    func.type = replace_implicit_first_type(functype, leading_type)
                elif has_self_type and isinstance(func.unanalyzed_type, CallableType):
                    if not isinstance(get_proper_type(func.unanalyzed_type.arg_types[0]), AnyType):
                        if self.is_expected_self_type(
                            self_type, func.is_class or func.name == "__new__"
                        ):
                            # This error is off by default, since it is explicitly allowed
                            # by the PEP 673.
                            self.fail(
                                'Redundant "Self" annotation for the first method argument',
                                func,
                                code=codes.REDUNDANT_SELF_TYPE,
                            )
                        else:
                            self.fail(
                                "Method cannot have explicit self annotation and Self type", func
                            )
        elif has_self_type:
            self.fail("Static methods cannot use Self type", func)

    def is_expected_self_type(self, typ: Type, is_classmethod: bool) -> bool:
        """Does this (analyzed or not) type represent the expected Self type for a method?"""
        assert self.type is not None
        typ = get_proper_type(typ)
        if is_classmethod:
            if isinstance(typ, TypeType):
                return self.is_expected_self_type(typ.item, is_classmethod=False)
            if isinstance(typ, UnboundType):
                sym = self.lookup_qualified(typ.name, typ, suppress_errors=True)
                if sym is not None and sym.fullname in TYPE_NAMES and typ.args:
                    return self.is_expected_self_type(typ.args[0], is_classmethod=False)
            return False
        if isinstance(typ, TypeVarType):
            return typ == self.type.self_type
        if isinstance(typ, UnboundType):
            sym = self.lookup_qualified(typ.name, typ, suppress_errors=True)
            return sym is not None and sym.fullname in SELF_TYPE_NAMES
        return False

    def set_original_def(self, previous: Node | None, new: FuncDef | Decorator) -> bool:
        """If 'new' conditionally redefine 'previous', set 'previous' as original

        We reject straight redefinitions of functions, as they are usually
        a programming error. For example:

          def f(): ...
          def f(): ...  # Error: 'f' redefined
        """
        if isinstance(new, Decorator):
            new = new.func
        if (
            isinstance(previous, (FuncDef, Decorator))
            and unnamed_function(new.name)
            and unnamed_function(previous.name)
        ):
            return True
        if isinstance(previous, (FuncDef, Var, Decorator)) and new.is_conditional:
            new.original_def = previous
            return True
        else:
            return False

    def update_function_type_variables(self, fun_type: CallableType, defn: FuncItem) -> bool:
        """Make any type variables in the signature of defn explicit.

        Update the signature of defn to contain type variable definitions
        if defn is generic. Return True, if the signature contains typing.Self
        type, or False otherwise.
        """
        fullname = self.function_fullname(defn.fullname)
        with self.tvar_scope_frame(self.tvar_scope.method_frame(fullname)):
            a = self.type_analyzer()
            fun_type.variables, has_self_type = a.bind_function_type_variables(fun_type, defn)
            if has_self_type and self.type is not None:
                self.setup_self_type()
            if defn.type_args:
                bound_fullnames = {v.fullname for v in fun_type.variables}
                declared_fullnames = {self.qualified_name(p.name) for p in defn.type_args}
                extra = sorted(bound_fullnames - declared_fullnames)
                if extra:
                    self.msg.type_parameters_should_be_declared(
                        [n.split(".")[-1] for n in extra], defn
                    )
            return has_self_type

    def setup_self_type(self) -> None:
        """Setup a (shared) Self type variable for current class.

        We intentionally don't add it to the class symbol table,
        so it can be accessed only by mypy and will not cause
        clashes with user defined names.
        """
        assert self.type is not None
        info = self.type
        if info.self_type is not None:
            if has_placeholder(info.self_type.upper_bound):
                # Similar to regular (user defined) type variables.
                self.process_placeholder(
                    None,
                    "Self upper bound",
                    info,
                    force_progress=info.self_type.upper_bound != fill_typevars(info),
                )
            else:
                return
        info.self_type = TypeVarType(
            "Self",
            f"{info.fullname}.Self",
            id=TypeVarId(0),  # 0 is a special value for self-types.
            values=[],
            upper_bound=fill_typevars(info),
            default=AnyType(TypeOfAny.from_omitted_generics),
        )

    def visit_overloaded_func_def(self, defn: OverloadedFuncDef) -> None:
        self.statement = defn
        self.add_function_to_symbol_table(defn)

        if not self.recurse_into_functions:
            return

        # NB: Since _visit_overloaded_func_def will call accept on the
        # underlying FuncDefs, the function might get entered twice.
        # This is fine, though, because only the outermost function is
        # used to compute targets.
        with self.scope.function_scope(defn):
            self.analyze_overloaded_func_def(defn)

    @contextmanager
    def overload_item_set(self, item: int | None) -> Iterator[None]:
        self.current_overload_item = item
        try:
            yield
        finally:
            self.current_overload_item = None

    def analyze_overloaded_func_def(self, defn: OverloadedFuncDef) -> None:
        # OverloadedFuncDef refers to any legitimate situation where you have
        # more than one declaration for the same function in a row.  This occurs
        # with a @property with a setter or a deleter, and for a classic
        # @overload.

        defn._fullname = self.qualified_name(defn.name)
        # TODO: avoid modifying items.
        defn.items = defn.unanalyzed_items.copy()

        first_item = defn.items[0]
        first_item.is_overload = True
        with self.overload_item_set(0):
            first_item.accept(self)

        bare_setter_type = None
        is_property = False
        if isinstance(first_item, Decorator) and first_item.func.is_property:
            is_property = True
            # This is a property.
            first_item.func.is_overload = True
            bare_setter_type = self.analyze_property_with_multi_part_definition(defn)
            typ = function_type(first_item.func, self.named_type("builtins.function"))
            assert isinstance(typ, CallableType)
            types = [typ]
        else:
            # This is a normal overload. Find the item signatures, the
            # implementation (if outside a stub), and any missing @overload
            # decorators.
            types, impl, non_overload_indexes = self.analyze_overload_sigs_and_impl(defn)
            defn.impl = impl
            if non_overload_indexes:
                self.handle_missing_overload_decorators(
                    defn, non_overload_indexes, some_overload_decorators=len(types) > 0
                )
            # If we found an implementation, remove it from the overload item list,
            # as it's special.
            if impl is not None:
                assert impl is defn.items[-1]
                defn.items = defn.items[:-1]
            elif not non_overload_indexes:
                self.handle_missing_overload_implementation(defn)

        if types and not any(
            # If some overload items are decorated with other decorators, then
            # the overload type will be determined during type checking.
            # Note: bare @property is removed in visit_decorator().
            isinstance(it, Decorator)
            and len(it.decorators) > (1 if i > 0 or not is_property else 0)
            for i, it in enumerate(defn.items)
        ):
            # TODO: should we enforce decorated overloads consistency somehow?
            # Some existing code uses both styles:
            #   * Put decorator only on implementation, use "effective" types in overloads
            #   * Put decorator everywhere, use "bare" types in overloads.
            defn.type = Overloaded(types)
            defn.type.line = defn.line
            # In addition, we can set the getter/setter type for valid properties as some
            # code paths may either use the above type, or var.type etc. of the first item.
            if isinstance(first_item, Decorator) and bare_setter_type:
                first_item.var.type = types[0]
                first_item.var.setter_type = bare_setter_type

        if not defn.items:
            # It was not a real overload after all, but function redefinition. We've
            # visited the redefinition(s) already.
            if not defn.impl:
                # For really broken overloads with no items and no implementation we need to keep
                # at least one item to hold basic information like function name.
                defn.impl = defn.unanalyzed_items[-1]
            return

        # We know this is an overload def. Infer properties and perform some checks.
        self.process_deprecated_overload(defn)
        self.process_final_in_overload(defn)
        self.process_static_or_class_method_in_overload(defn)
        self.process_overload_impl(defn)

    def process_deprecated_overload(self, defn: OverloadedFuncDef) -> None:
        if defn.is_property:
            return

        if isinstance(impl := defn.impl, Decorator) and (
            (deprecated := impl.func.deprecated) is not None
        ):
            defn.deprecated = deprecated
            for item in defn.items:
                if isinstance(item, Decorator):
                    item.func.deprecated = deprecated

        for item in defn.items:
            deprecation = False
            if isinstance(item, Decorator):
                for d in item.decorators:
                    if deprecation and refers_to_fullname(d, OVERLOAD_NAMES):
                        self.msg.note("@overload should be placed before @deprecated", d)
                    elif (deprecated := self.get_deprecated(d)) is not None:
                        deprecation = True
                        if isinstance(typ := item.func.type, CallableType):
                            typestr = f" {typ} "
                        else:
                            typestr = " "
                        item.func.deprecated = (
                            f"overload{typestr}of function {defn.fullname} is deprecated: "
                            f"{deprecated}"
                        )

    @staticmethod
    def get_deprecated(expression: Expression) -> str | None:
        if (
            isinstance(expression, CallExpr)
            and refers_to_fullname(expression.callee, DEPRECATED_TYPE_NAMES)
            and (len(args := expression.args) >= 1)
            and isinstance(deprecated := args[0], StrExpr)
        ):
            return deprecated.value
        return None

    def process_overload_impl(self, defn: OverloadedFuncDef) -> None:
        """Set flags for an overload implementation.

        Currently, this checks for a trivial body in protocols classes,
        where it makes the method implicitly abstract.
        """
        if defn.impl is None:
            return
        impl = defn.impl if isinstance(defn.impl, FuncDef) else defn.impl.func
        if is_trivial_body(impl.body) and self.is_class_scope() and not self.is_stub_file:
            assert self.type is not None
            if self.type.is_protocol:
                impl.abstract_status = IMPLICITLY_ABSTRACT
            if impl.abstract_status != NOT_ABSTRACT:
                impl.is_trivial_body = True

    def analyze_overload_sigs_and_impl(
        self, defn: OverloadedFuncDef
    ) -> tuple[list[CallableType], OverloadPart | None, list[int]]:
        """Find overload signatures, the implementation, and items with missing @overload.

        Assume that the first was already analyzed. As a side effect:
        analyzes remaining items and updates 'is_overload' flags.
        """
        types = []
        non_overload_indexes = []
        impl: OverloadPart | None = None
        for i, item in enumerate(defn.items):
            if i != 0:
                # Assume that the first item was already visited
                item.is_overload = True
                with self.overload_item_set(i if i < len(defn.items) - 1 else None):
                    item.accept(self)
            # TODO: support decorated overloaded functions properly
            if isinstance(item, Decorator):
                callable = function_type(item.func, self.named_type("builtins.function"))
                assert isinstance(callable, CallableType)
                if not any(refers_to_fullname(dec, OVERLOAD_NAMES) for dec in item.decorators):
                    if i == len(defn.items) - 1 and not self.is_stub_file:
                        # Last item outside a stub is impl
                        impl = item
                    else:
                        # Oops it wasn't an overload after all. A clear error
                        # will vary based on where in the list it is, record
                        # that.
                        non_overload_indexes.append(i)
                else:
                    item.func.is_overload = True
                    types.append(callable)
                    if item.var.is_property:
                        self.fail("An overload can not be a property", item)
                # If any item was decorated with `@override`, the whole overload
                # becomes an explicit override.
                defn.is_explicit_override |= item.func.is_explicit_override
            elif isinstance(item, FuncDef):
                if i == len(defn.items) - 1 and not self.is_stub_file:
                    impl = item
                else:
                    non_overload_indexes.append(i)
        return types, impl, non_overload_indexes

    def handle_missing_overload_decorators(
        self,
        defn: OverloadedFuncDef,
        non_overload_indexes: list[int],
        some_overload_decorators: bool,
    ) -> None:
        """Generate errors for overload items without @overload.

        Side effect: remote non-overload items.
        """
        if some_overload_decorators:
            # Some of them were overloads, but not all.
            for idx in non_overload_indexes:
                if self.is_stub_file:
                    self.fail(
                        "An implementation for an overloaded function "
                        "is not allowed in a stub file",
                        defn.items[idx],
                    )
                else:
                    self.fail(
                        "The implementation for an overloaded function must come last",
                        defn.items[idx],
                    )
        else:
            for idx in non_overload_indexes[1:]:
                self.name_already_defined(defn.name, defn.items[idx], defn.items[0])
            if defn.impl:
                self.name_already_defined(defn.name, defn.impl, defn.items[0])
        # Remove the non-overloads
        for idx in reversed(non_overload_indexes):
            del defn.items[idx]

    def handle_missing_overload_implementation(self, defn: OverloadedFuncDef) -> None:
        """Generate error about missing overload implementation (only if needed)."""
        if not self.is_stub_file:
            if self.type and self.type.is_protocol and not self.is_func_scope():
                # An overloaded protocol method doesn't need an implementation,
                # but if it doesn't have one, then it is considered abstract.
                for item in defn.items:
                    if isinstance(item, Decorator):
                        item.func.abstract_status = IS_ABSTRACT
                    else:
                        item.abstract_status = IS_ABSTRACT
            elif all(
                isinstance(item, Decorator) and item.func.abstract_status == IS_ABSTRACT
                for item in defn.items
            ):
                # Since there is no implementation, it can't be called via super().
                if defn.items:
                    assert isinstance(defn.items[0], Decorator)
                    defn.items[0].func.is_trivial_body = True
            else:
                self.fail(
                    "An overloaded function outside a stub file must have an implementation",
                    defn,
                    code=codes.NO_OVERLOAD_IMPL,
                )

    def process_final_in_overload(self, defn: OverloadedFuncDef) -> None:
        """Detect the @final status of an overloaded function (and perform checks)."""
        # If the implementation is marked as @final (or the first overload in
        # stubs), then the whole overloaded definition if @final.
        if any(item.is_final for item in defn.items):
            # We anyway mark it as final because it was probably the intention.
            defn.is_final = True
            # Only show the error once per overload
            bad_final = next(ov for ov in defn.items if ov.is_final)
            if not self.is_stub_file:
                self.fail("@final should be applied only to overload implementation", bad_final)
            elif any(item.is_final for item in defn.items[1:]):
                bad_final = next(ov for ov in defn.items[1:] if ov.is_final)
                self.fail(
                    "In a stub file @final must be applied only to the first overload", bad_final
                )
        if defn.impl is not None and defn.impl.is_final:
            defn.is_final = True

    def process_static_or_class_method_in_overload(self, defn: OverloadedFuncDef) -> None:
        class_status = []
        static_status = []
        for item in defn.items:
            if isinstance(item, Decorator):
                inner = item.func
            elif isinstance(item, FuncDef):
                inner = item
            else:
                assert False, f"The 'item' variable is an unexpected type: {type(item)}"
            class_status.append(inner.is_class)
            static_status.append(inner.is_static)

        if defn.impl is not None:
            if isinstance(defn.impl, Decorator):
                inner = defn.impl.func
            elif isinstance(defn.impl, FuncDef):
                inner = defn.impl
            else:
                assert False, f"Unexpected impl type: {type(defn.impl)}"
            class_status.append(inner.is_class)
            static_status.append(inner.is_static)

        if len(set(class_status)) != 1:
            self.msg.overload_inconsistently_applies_decorator("classmethod", defn)
        elif len(set(static_status)) != 1:
            self.msg.overload_inconsistently_applies_decorator("staticmethod", defn)
        else:
            defn.is_class = class_status[0]
            defn.is_static = static_status[0]

    def analyze_property_with_multi_part_definition(
        self, defn: OverloadedFuncDef
    ) -> CallableType | None:
        """Analyze a property defined using multiple methods (e.g., using @x.setter).

        Assume that the first method (@property) has already been analyzed.
        Return bare setter type (without any other decorators applied), this may be used
        by the caller for performance optimizations.
        """
        defn.is_property = True
        items = defn.items
        first_item = defn.items[0]
        assert isinstance(first_item, Decorator)
        deleted_items = []
        bare_setter_type = None
        func_name = first_item.func.name
        for i, item in enumerate(items[1:]):
            if isinstance(item, Decorator):
                item.func.accept(self)
                if item.decorators:
                    first_node = item.decorators[0]
                    if self._is_valid_property_decorator(first_node, func_name):
                        # Get abstractness from the original definition.
                        item.func.abstract_status = first_item.func.abstract_status
                        if first_node.name == "setter":
                            # The first item represents the entire property.
                            first_item.var.is_settable_property = True
                            setter_func_type = function_type(
                                item.func, self.named_type("builtins.function")
                            )
                            assert isinstance(setter_func_type, CallableType)
                            bare_setter_type = setter_func_type
                            defn.setter_index = i + 1
                        for other_node in item.decorators[1:]:
                            other_node.accept(self)
                    else:
                        self.fail(
                            f'Only supported top decorators are "@{func_name}.setter" and "@{func_name}.deleter"',
                            first_node,
                        )
            else:
                self.fail(f'Unexpected definition for property "{func_name}"', item)
                deleted_items.append(i + 1)
        for i in reversed(deleted_items):
            del items[i]

        for item in items[1:]:
            if isinstance(item, Decorator):
                for d in item.decorators:
                    if (deprecated := self.get_deprecated(d)) is not None:
                        item.func.deprecated = (
                            f"function {item.fullname} is deprecated: {deprecated}"
                        )
        return bare_setter_type

    def _is_valid_property_decorator(
        self, deco: Expression, property_name: str
    ) -> TypeGuard[MemberExpr]:
        if not isinstance(deco, MemberExpr):
            return False
        if not isinstance(deco.expr, NameExpr) or deco.expr.name != property_name:
            return False
        if deco.name not in {"setter", "deleter"}:
            # This intentionally excludes getter. While `@prop.getter` is valid at
            # runtime, that would mean replacing the already processed getter type.
            # Such usage is almost definitely a mistake (except for overrides in
            # subclasses but we don't support them anyway) and might be a typo
            # (only one letter away from `setter`), it's likely almost never used,
            # so supporting it properly won't pay off.
            return False
        return True

    def add_function_to_symbol_table(self, func: FuncDef | OverloadedFuncDef) -> None:
        if self.is_class_scope():
            assert self.type is not None
            func.info = self.type
        func._fullname = self.qualified_name(func.name)
        self.add_symbol(func.name, func, func)

    def analyze_arg_initializers(self, defn: FuncItem) -> None:
        fullname = self.function_fullname(defn.fullname)
        with self.tvar_scope_frame(self.tvar_scope.method_frame(fullname)):
            # Analyze default arguments
            for arg in defn.arguments:
                if arg.initializer:
                    arg.initializer.accept(self)

    def analyze_function_body(self, defn: FuncItem) -> None:
        is_method = self.is_class_scope()
        fullname = self.function_fullname(defn.fullname)
        with self.tvar_scope_frame(self.tvar_scope.method_frame(fullname)):
            # Bind the type variables again to visit the body.
            if defn.type:
                a = self.type_analyzer()
                typ = defn.type
                assert isinstance(typ, CallableType)
                a.bind_function_type_variables(typ, defn)
                for i in range(len(typ.arg_types)):
                    store_argument_type(defn, i, typ, self.named_type)
            self.function_stack.append(defn)
            with self.enter(defn):
                for arg in defn.arguments:
                    self.add_local(arg.variable, defn)

                # The first argument of a non-static, non-class method is like 'self'
                # (though the name could be different), having the enclosing class's
                # instance type.
                if is_method and defn.has_self_or_cls_argument and defn.arguments:
                    if not defn.is_class:
                        defn.arguments[0].variable.is_self = True
                    else:
                        defn.arguments[0].variable.is_cls = True

                defn.body.accept(self)
            self.function_stack.pop()

    def check_classvar_in_signature(self, typ: ProperType) -> None:
        t: ProperType
        if isinstance(typ, Overloaded):
            for t in typ.items:
                self.check_classvar_in_signature(t)
            return
        if not isinstance(typ, CallableType):
            return
        for t in get_proper_types(typ.arg_types) + [get_proper_type(typ.ret_type)]:
            if self.is_classvar(t):
                self.fail_invalid_classvar(t)
                # Show only one error per signature
                break

    def check_function_signature(self, fdef: FuncItem) -> None:
        sig = fdef.type
        assert isinstance(sig, CallableType)
        if len(sig.arg_types) < len(fdef.arguments):
            self.fail("Type signature has too few arguments", fdef)
            # Add dummy Any arguments to prevent crashes later.
            num_extra_anys = len(fdef.arguments) - len(sig.arg_types)
            extra_anys = [AnyType(TypeOfAny.from_error)] * num_extra_anys
            sig.arg_types.extend(extra_anys)
        elif len(sig.arg_types) > len(fdef.arguments):
            self.fail("Type signature has too many arguments", fdef, blocker=True)

    def visit_decorator(self, dec: Decorator) -> None:
        self.statement = dec
        # TODO: better don't modify them at all.
        dec.decorators = dec.original_decorators.copy()
        dec.func.is_conditional = self.block_depth[-1] > 0
        if not dec.is_overload:
            self.add_symbol(dec.name, dec, dec)
        dec.func._fullname = self.qualified_name(dec.name)
        dec.var._fullname = self.qualified_name(dec.name)
        for d in dec.decorators:
            d.accept(self)
        removed: list[int] = []
        no_type_check = False
        could_be_decorated_property = False
        for i, d in enumerate(dec.decorators):
            # A bunch of decorators are special cased here.
            if refers_to_fullname(d, "abc.abstractmethod"):
                removed.append(i)
                dec.func.abstract_status = IS_ABSTRACT
                self.check_decorated_function_is_method("abstractmethod", dec)
            elif refers_to_fullname(d, ("asyncio.coroutines.coroutine", "types.coroutine")):
                removed.append(i)
                dec.func.is_awaitable_coroutine = True
            elif refers_to_fullname(d, "builtins.staticmethod"):
                removed.append(i)
                dec.func.is_static = True
                dec.var.is_staticmethod = True
                self.check_decorated_function_is_method("staticmethod", dec)
            elif refers_to_fullname(d, "builtins.classmethod"):
                removed.append(i)
                dec.func.is_class = True
                dec.var.is_classmethod = True
                self.check_decorated_function_is_method("classmethod", dec)
            elif refers_to_fullname(d, OVERRIDE_DECORATOR_NAMES):
                removed.append(i)
                dec.func.is_explicit_override = True
                self.check_decorated_function_is_method("override", dec)
            elif refers_to_fullname(
                d,
                (
                    "builtins.property",
                    "abc.abstractproperty",
                    "functools.cached_property",
                    "enum.property",
                    "types.DynamicClassAttribute",
                ),
            ):
                removed.append(i)
                dec.func.is_property = True
                dec.var.is_property = True
                if refers_to_fullname(d, "abc.abstractproperty"):
                    dec.func.abstract_status = IS_ABSTRACT
                elif refers_to_fullname(d, "functools.cached_property"):
                    dec.var.is_settable_property = True
                self.check_decorated_function_is_method("property", dec)
            elif refers_to_fullname(d, "typing.no_type_check"):
                dec.var.type = AnyType(TypeOfAny.special_form)
                no_type_check = True
            elif refers_to_fullname(d, FINAL_DECORATOR_NAMES):
                if self.is_class_scope():
                    assert self.type is not None, "No type set at class scope"
                    if self.type.is_protocol:
                        self.msg.protocol_members_cant_be_final(d)
                    else:
                        dec.func.is_final = True
                        dec.var.is_final = True
                    removed.append(i)
                else:
                    self.fail("@final cannot be used with non-method functions", d)
            elif refers_to_fullname(d, TYPE_CHECK_ONLY_NAMES):
                # TODO: support `@overload` funcs.
                dec.func.is_type_check_only = True
            elif isinstance(d, CallExpr) and refers_to_fullname(
                d.callee, DATACLASS_TRANSFORM_NAMES
            ):
                dec.func.dataclass_transform_spec = self.parse_dataclass_transform_spec(d)
            elif (deprecated := self.get_deprecated(d)) is not None:
                dec.func.deprecated = f"function {dec.fullname} is deprecated: {deprecated}"
            elif not dec.var.is_property:
                # We have seen a "non-trivial" decorator before seeing @property, if
                # we will see a @property later, give an error, as we don't support this.
                could_be_decorated_property = True
        for i in reversed(removed):
            del dec.decorators[i]
        if (not dec.is_overload or dec.var.is_property) and self.type:
            dec.var.info = self.type
            dec.var.is_initialized_in_class = True
        if not no_type_check and self.recurse_into_functions:
            dec.func.accept(self)
        if could_be_decorated_property and dec.decorators and dec.var.is_property:
            self.fail(
                "Decorators on top of @property are not supported", dec, code=PROPERTY_DECORATOR
            )
        if (dec.func.is_static or dec.func.is_class) and dec.var.is_property:
            self.fail("Only instance methods can be decorated with @property", dec)
        if dec.func.abstract_status == IS_ABSTRACT and dec.func.is_final:
            self.fail(f"Method {dec.func.name} is both abstract and final", dec)
        if dec.func.is_static and dec.func.is_class:
            self.fail(message_registry.CLASS_PATTERN_CLASS_OR_STATIC_METHOD, dec)

    def check_decorated_function_is_method(self, decorator: str, context: Context) -> None:
        if not self.type or self.is_func_scope():
            self.fail(f'"{decorator}" used with a non-method', context)

    #
    # Classes
    #

    def visit_class_def(self, defn: ClassDef) -> None:
        self.statement = defn
        self.incomplete_type_stack.append(not defn.info)
        namespace = self.qualified_name(defn.name)
        with self.tvar_scope_frame(self.tvar_scope.class_frame(namespace)):
            if self.push_type_args(defn.type_args, defn) is None:
                self.mark_incomplete(defn.name, defn)
                return

            self.analyze_class(defn)
            self.pop_type_args(defn.type_args)
        self.incomplete_type_stack.pop()

    def push_type_args(
        self, type_args: list[TypeParam] | None, context: Context
    ) -> list[tuple[str, TypeVarLikeExpr]] | None:
        if not type_args:
            return []
        self.locals.append(SymbolTable())
        self.scope_stack.append(SCOPE_ANNOTATION)
        tvs: list[tuple[str, TypeVarLikeExpr]] = []
        for p in type_args:
            tv = self.analyze_type_param(p, context)
            if tv is None:
                return None
            tvs.append((p.name, tv))

        for name, tv in tvs:
            if self.is_defined_type_param(name):
                self.fail(f'"{name}" already defined as a type parameter', context)
            else:
                self.add_symbol(name, tv, context, no_progress=True, type_param=True)

        return tvs

    def is_defined_type_param(self, name: str) -> bool:
        for names in self.locals:
            if names is None:
                continue
            if name in names:
                node = names[name].node
                if isinstance(node, TypeVarLikeExpr):
                    return True
        return False

    def analyze_type_param(
        self, type_param: TypeParam, context: Context
    ) -> TypeVarLikeExpr | None:
        fullname = self.qualified_name(type_param.name)
        if type_param.upper_bound:
            upper_bound = self.anal_type(type_param.upper_bound, allow_placeholder=True)
            # TODO: we should validate the upper bound is valid for a given kind.
            if upper_bound is None:
                # This and below copies special-casing for old-style type variables, that
                # is equally necessary for new-style classes to break a vicious circle.
                upper_bound = PlaceholderType(None, [], context.line)
        else:
            if type_param.kind == TYPE_VAR_TUPLE_KIND:
                upper_bound = self.named_type("builtins.tuple", [self.object_type()])
            else:
                upper_bound = self.object_type()
        if type_param.default:
            default = self.anal_type(
                type_param.default,
                allow_placeholder=True,
                allow_unbound_tvars=True,
                report_invalid_types=False,
                allow_param_spec_literals=type_param.kind == PARAM_SPEC_KIND,
                allow_tuple_literal=type_param.kind == PARAM_SPEC_KIND,
                allow_unpack=type_param.kind == TYPE_VAR_TUPLE_KIND,
            )
            if default is None:
                default = PlaceholderType(None, [], context.line)
            elif type_param.kind == TYPE_VAR_KIND:
                default = self.check_typevar_default(default, type_param.default)
            elif type_param.kind == PARAM_SPEC_KIND:
                default = self.check_paramspec_default(default, type_param.default)
            elif type_param.kind == TYPE_VAR_TUPLE_KIND:
                default = self.check_typevartuple_default(default, type_param.default)
        else:
            default = AnyType(TypeOfAny.from_omitted_generics)
        if type_param.kind == TYPE_VAR_KIND:
            values: list[Type] = []
            if type_param.values:
                for value in type_param.values:
                    analyzed = self.anal_type(value, allow_placeholder=True)
                    if analyzed is None:
                        analyzed = PlaceholderType(None, [], context.line)
                    if has_type_vars(analyzed):
                        self.fail(message_registry.TYPE_VAR_GENERIC_CONSTRAINT_TYPE, context)
                        values.append(AnyType(TypeOfAny.from_error))
                    else:
                        values.append(analyzed)
            return TypeVarExpr(
                name=type_param.name,
                fullname=fullname,
                values=values,
                upper_bound=upper_bound,
                default=default,
                variance=VARIANCE_NOT_READY,
                is_new_style=True,
                line=context.line,
            )
        elif type_param.kind == PARAM_SPEC_KIND:
            return ParamSpecExpr(
                name=type_param.name,
                fullname=fullname,
                upper_bound=upper_bound,
                default=default,
                is_new_style=True,
                line=context.line,
            )
        else:
            assert type_param.kind == TYPE_VAR_TUPLE_KIND
            tuple_fallback = self.named_type("builtins.tuple", [self.object_type()])
            return TypeVarTupleExpr(
                name=type_param.name,
                fullname=fullname,
                upper_bound=upper_bound,
                tuple_fallback=tuple_fallback,
                default=default,
                is_new_style=True,
                line=context.line,
            )

    def pop_type_args(self, type_args: list[TypeParam] | None) -> None:
        if not type_args:
            return
        self.locals.pop()
        self.scope_stack.pop()

    def analyze_class(self, defn: ClassDef) -> None:
        fullname = self.qualified_name(defn.name)
        if not defn.info and not self.is_core_builtin_class(defn):
            # Add placeholder so that self-references in base classes can be
            # resolved.  We don't want this to cause a deferral, since if there
            # are no incomplete references, we'll replace this with a TypeInfo
            # before returning.
            placeholder = PlaceholderNode(fullname, defn, defn.line, becomes_typeinfo=True)
            self.add_symbol(defn.name, placeholder, defn, can_defer=False)

        tag = self.track_incomplete_refs()

        # Restore base classes after previous iteration (things like Generic[T] might be removed).
        defn.base_type_exprs.extend(defn.removed_base_type_exprs)
        defn.removed_base_type_exprs.clear()

        self.infer_metaclass_and_bases_from_compat_helpers(defn)

        bases = defn.base_type_exprs
        bases, tvar_defs, is_protocol = self.clean_up_bases_and_infer_type_variables(
            defn, bases, context=defn
        )

        self.check_type_alias_bases(bases)

        for tvd in tvar_defs:
            if isinstance(tvd, TypeVarType) and any(
                has_placeholder(t) for t in [tvd.upper_bound] + tvd.values
            ):
                # Some type variable bounds or values are not ready, we need
                # to re-analyze this class.
                self.defer()
            if has_placeholder(tvd.default):
                # Placeholder values in TypeVarLikeTypes may get substituted in.
                # Defer current target until they are ready.
                self.mark_incomplete(defn.name, defn)
                return

        self.analyze_class_keywords(defn)
        bases_result = self.analyze_base_classes(bases)
        if bases_result is None or self.found_incomplete_ref(tag):
            # Something was incomplete. Defer current target.
            self.mark_incomplete(defn.name, defn)
            return

        base_types, base_error = bases_result
        if any(isinstance(base, PlaceholderType) for base, _ in base_types):
            # We need to know the TypeInfo of each base to construct the MRO. Placeholder types
            # are okay in nested positions, since they can't affect the MRO.
            self.mark_incomplete(defn.name, defn)
            return

        declared_metaclass, should_defer, any_meta = self.get_declared_metaclass(
            defn.name, defn.metaclass
        )
        if should_defer or self.found_incomplete_ref(tag):
            # Metaclass was not ready. Defer current target.
            self.mark_incomplete(defn.name, defn)
            return

        if self.analyze_typeddict_classdef(defn):
            if defn.info:
                self.setup_type_vars(defn, tvar_defs)
                self.setup_alias_type_vars(defn)
            return

        if self.analyze_namedtuple_classdef(defn, tvar_defs):
            return

        # Create TypeInfo for class now that base classes and the MRO can be calculated.
        self.prepare_class_def(defn)
        self.setup_type_vars(defn, tvar_defs)
        if base_error:
            defn.info.fallback_to_any = True
        if any_meta:
            defn.info.meta_fallback_to_any = True

        with self.scope.class_scope(defn.info):
            self.configure_base_classes(defn, base_types)
            defn.info.is_protocol = is_protocol
            self.recalculate_metaclass(defn, declared_metaclass)
            defn.info.runtime_protocol = False

            if defn.type_args:
                # PEP 695 type parameters are not in scope in class decorators, so
                # temporarily disable type parameter namespace.
                type_params_names = self.locals.pop()
                self.scope_stack.pop()
            for decorator in defn.decorators:
                self.analyze_class_decorator(defn, decorator)
            if defn.type_args:
                self.locals.append(type_params_names)
                self.scope_stack.append(SCOPE_ANNOTATION)

            self.analyze_class_body_common(defn)

    def check_type_alias_bases(self, bases: list[Expression]) -> None:
        for base in bases:
            if isinstance(base, IndexExpr):
                base = base.base
            if (
                isinstance(base, RefExpr)
                and isinstance(base.node, TypeAlias)
                and base.node.python_3_12_type_alias
            ):
                self.fail(
                    'Type alias defined using "type" statement not valid as base class', base
                )

    def setup_type_vars(self, defn: ClassDef, tvar_defs: list[TypeVarLikeType]) -> None:
        defn.type_vars = tvar_defs
        defn.info.type_vars = []
        # we want to make sure any additional logic in add_type_vars gets run
        defn.info.add_type_vars()

    def setup_alias_type_vars(self, defn: ClassDef) -> None:
        assert defn.info.special_alias is not None
        defn.info.special_alias.alias_tvars = list(defn.type_vars)
        # It is a bit unfortunate that we need to inline some logic from TypeAlias constructor,
        # but it is required, since type variables may change during semantic analyzer passes.
        for i, t in enumerate(defn.type_vars):
            if isinstance(t, TypeVarTupleType):
                defn.info.special_alias.tvar_tuple_index = i
        target = defn.info.special_alias.target
        assert isinstance(target, ProperType)
        if isinstance(target, TypedDictType):
            target.fallback.args = type_vars_as_args(defn.type_vars)
        elif isinstance(target, TupleType):
            target.partial_fallback.args = type_vars_as_args(defn.type_vars)
        else:
            assert False, f"Unexpected special alias type: {type(target)}"

    def is_core_builtin_class(self, defn: ClassDef) -> bool:
        return self.cur_mod_id == "builtins" and defn.name in CORE_BUILTIN_CLASSES

    def analyze_class_body_common(self, defn: ClassDef) -> None:
        """Parts of class body analysis that are common to all kinds of class defs."""
        self.enter_class(defn.info)
        if any(b.self_type is not None for b in defn.info.mro):
            self.setup_self_type()
        defn.defs.accept(self)
        self.apply_class_plugin_hooks(defn)
        self.leave_class()

    def analyze_typeddict_classdef(self, defn: ClassDef) -> bool:
        if (
            defn.info
            and defn.info.typeddict_type
            and not has_placeholder(defn.info.typeddict_type)
        ):
            # This is a valid TypedDict, and it is fully analyzed.
            return True
        is_typeddict, info = self.typed_dict_analyzer.analyze_typeddict_classdef(defn)
        if is_typeddict:
            for decorator in defn.decorators:
                decorator.accept(self)
                if info is not None:
                    self.analyze_class_decorator_common(defn, info, decorator)
            if info is None:
                self.mark_incomplete(defn.name, defn)
            else:
                self.prepare_class_def(defn, info, custom_names=True)
            return True
        return False

    def analyze_namedtuple_classdef(
        self, defn: ClassDef, tvar_defs: list[TypeVarLikeType]
    ) -> bool:
        """Check if this class can define a named tuple."""
        if (
            defn.info
            and defn.info.is_named_tuple
            and defn.info.tuple_type
            and not has_placeholder(defn.info.tuple_type)
        ):
            # Don't reprocess everything. We just need to process methods defined
            # in the named tuple class body.
            is_named_tuple = True
            info: TypeInfo | None = defn.info
        else:
            is_named_tuple, info = self.named_tuple_analyzer.analyze_namedtuple_classdef(
                defn, self.is_stub_file, self.is_func_scope()
            )
        if is_named_tuple:
            if info is None:
                self.mark_incomplete(defn.name, defn)
            else:
                self.prepare_class_def(defn, info, custom_names=True)
                self.setup_type_vars(defn, tvar_defs)
                self.setup_alias_type_vars(defn)
                with self.scope.class_scope(defn.info):
                    for deco in defn.decorators:
                        deco.accept(self)
                        self.analyze_class_decorator_common(defn, defn.info, deco)
                    with self.named_tuple_analyzer.save_namedtuple_body(info):
                        self.analyze_class_body_common(defn)
            return True
        return False

    def apply_class_plugin_hooks(self, defn: ClassDef) -> None:
        """Apply a plugin hook that may infer a more precise definition for a class."""

        for decorator in defn.decorators:
            decorator_name = self.get_fullname_for_hook(decorator)
            if decorator_name:
                hook = self.plugin.get_class_decorator_hook(decorator_name)
                # Special case: if the decorator is itself decorated with
                # typing.dataclass_transform, apply the hook for the dataclasses plugin
                # TODO: remove special casing here
                if hook is None and find_dataclass_transform_spec(decorator):
                    hook = dataclasses_plugin.dataclass_tag_callback
                if hook:
                    hook(ClassDefContext(defn, decorator, self))

        if defn.metaclass:
            metaclass_name = self.get_fullname_for_hook(defn.metaclass)
            if metaclass_name:
                hook = self.plugin.get_metaclass_hook(metaclass_name)
                if hook:
                    hook(ClassDefContext(defn, defn.metaclass, self))

        for base_expr in defn.base_type_exprs:
            base_name = self.get_fullname_for_hook(base_expr)
            if base_name:
                hook = self.plugin.get_base_class_hook(base_name)
                if hook:
                    hook(ClassDefContext(defn, base_expr, self))

        # Check if the class definition itself triggers a dataclass transform (via a parent class/
        # metaclass)
        spec = find_dataclass_transform_spec(defn)
        if spec is not None:
            dataclasses_plugin.add_dataclass_tag(defn.info)

    def get_fullname_for_hook(self, expr: Expression) -> str | None:
        if isinstance(expr, CallExpr):
            return self.get_fullname_for_hook(expr.callee)
        elif isinstance(expr, IndexExpr):
            return self.get_fullname_for_hook(expr.base)
        elif isinstance(expr, RefExpr):
            if expr.fullname:
                return expr.fullname
            # If we don't have a fullname look it up. This happens because base classes are
            # analyzed in a different manner (see exprtotype.py) and therefore those AST
            # nodes will not have full names.
            sym = self.lookup_type_node(expr)
            if sym:
                return sym.fullname
        return None

    def analyze_class_keywords(self, defn: ClassDef) -> None:
        for value in defn.keywords.values():
            value.accept(self)

    def enter_class(self, info: TypeInfo) -> None:
        # Remember previous active class
        self.type_stack.append(self.type)
        self.locals.append(None)  # Add class scope
        self.scope_stack.append(SCOPE_CLASS)
        self.block_depth.append(-1)  # The class body increments this to 0
        self.loop_depth.append(0)
        self._type = info
        self.missing_names.append(set())

    def leave_class(self) -> None:
        """Restore analyzer state."""
        self.block_depth.pop()
        self.loop_depth.pop()
        self.locals.pop()
        self.scope_stack.pop()
        self._type = self.type_stack.pop()
        self.missing_names.pop()

    def analyze_class_decorator(self, defn: ClassDef, decorator: Expression) -> None:
        decorator.accept(self)
        self.analyze_class_decorator_common(defn, defn.info, decorator)
        if isinstance(decorator, RefExpr):
            if decorator.fullname in RUNTIME_PROTOCOL_DECOS:
                if defn.info.is_protocol:
                    defn.info.runtime_protocol = True
                else:
                    self.fail("@runtime_checkable can only be used with protocol classes", defn)
        elif isinstance(decorator, CallExpr) and refers_to_fullname(
            decorator.callee, DATACLASS_TRANSFORM_NAMES
        ):
            defn.info.dataclass_transform_spec = self.parse_dataclass_transform_spec(decorator)

    def analyze_class_decorator_common(
        self, defn: ClassDef, info: TypeInfo, decorator: Expression
    ) -> None:
        """Common method for applying class decorators.

        Called on regular classes, typeddicts, and namedtuples.
        """
        if refers_to_fullname(decorator, FINAL_DECORATOR_NAMES):
            info.is_final = True
        elif refers_to_fullname(decorator, TYPE_CHECK_ONLY_NAMES):
            info.is_type_check_only = True
        elif (deprecated := self.get_deprecated(decorator)) is not None:
            info.deprecated = f"class {defn.fullname} is deprecated: {deprecated}"

    def clean_up_bases_and_infer_type_variables(
        self, defn: ClassDef, base_type_exprs: list[Expression], context: Context
    ) -> tuple[list[Expression], list[TypeVarLikeType], bool]:
        """Remove extra base classes such as Generic and infer type vars.

        For example, consider this class:

          class Foo(Bar, Generic[T]): ...

        Now we will remove Generic[T] from bases of Foo and infer that the
        type variable 'T' is a type argument of Foo.

        Note that this is performed *before* semantic analysis.

        Returns (remaining base expressions, inferred type variables, is protocol).
        """
        removed: list[int] = []
        declared_tvars: TypeVarLikeList = []
        is_protocol = False
        if defn.type_args is not None:
            for p in defn.type_args:
                node = self.lookup(p.name, context)
                assert node is not None
                assert isinstance(node.node, TypeVarLikeExpr)
                declared_tvars.append((p.name, node.node))

        for i, base_expr in enumerate(base_type_exprs):
            if isinstance(base_expr, StarExpr):
                base_expr.valid = True
            self.analyze_type_expr(base_expr)

            try:
                base = self.expr_to_unanalyzed_type(base_expr)
            except TypeTranslationError:
                # This error will be caught later.
                continue
            result = self.analyze_class_typevar_declaration(base)
            if result is not None:
                tvars = result[0]
                is_protocol |= result[1]
                if declared_tvars:
                    if defn.type_args:
                        if is_protocol:
                            self.fail('No arguments expected for "Protocol" base class', context)
                        else:
                            self.fail("Generic[...] base class is redundant", context)
                    else:
                        self.fail(
                            "Only single Generic[...] or Protocol[...] can be in bases", context
                        )
                removed.append(i)
                declared_tvars.extend(tvars)
            if isinstance(base, UnboundType):
                sym = self.lookup_qualified(base.name, base)
                if sym is not None and sym.node is not None:
                    if sym.node.fullname in PROTOCOL_NAMES and i not in removed:
                        # also remove bare 'Protocol' bases
                        removed.append(i)
                        is_protocol = True

        all_tvars = self.get_all_bases_tvars(base_type_exprs, removed)
        if declared_tvars:
            if len(remove_dups(declared_tvars)) < len(declared_tvars) and not defn.type_args:
                self.fail("Duplicate type variables in Generic[...] or Protocol[...]", context)
            declared_tvars = remove_dups(declared_tvars)
            if not set(all_tvars).issubset(set(declared_tvars)):
                if defn.type_args:
                    undeclared = sorted(set(all_tvars) - set(declared_tvars))
                    self.msg.type_parameters_should_be_declared(
                        [tv[0] for tv in undeclared], context
                    )
                else:
                    self.fail(
                        "If Generic[...] or Protocol[...] is present"
                        " it should list all type variables",
                        context,
                    )
                # In case of error, Generic tvars will go first
                declared_tvars = remove_dups(declared_tvars + all_tvars)
        else:
            declared_tvars = all_tvars
        for i in reversed(removed):
            # We need to actually remove the base class expressions like Generic[T],
            # mostly because otherwise they will create spurious dependencies in fine
            # grained incremental mode.
            defn.removed_base_type_exprs.append(defn.base_type_exprs[i])
            del base_type_exprs[i]
        tvar_defs = self.tvar_defs_from_tvars(declared_tvars, context)
        return base_type_exprs, tvar_defs, is_protocol

    def analyze_class_typevar_declaration(self, base: Type) -> tuple[TypeVarLikeList, bool] | None:
        """Analyze type variables declared using Generic[...] or Protocol[...].

        Args:
            base: Non-analyzed base class

        Return None if the base class does not declare type variables. Otherwise,
        return the type variables.
        """
        if not isinstance(base, UnboundType):
            return None
        unbound = base
        sym = self.lookup_qualified(unbound.name, unbound)
        if sym is None or sym.node is None:
            return None
        if (
            sym.node.fullname == "typing.Generic"
            or sym.node.fullname in PROTOCOL_NAMES
            and base.args
        ):
            is_proto = sym.node.fullname != "typing.Generic"
            tvars: TypeVarLikeList = []
            have_type_var_tuple = False
            for arg in unbound.args:
                tag = self.track_incomplete_refs()
                tvar = self.analyze_unbound_tvar(arg)
                if tvar:
                    if isinstance(tvar[1], TypeVarTupleExpr):
                        if have_type_var_tuple:
                            self.fail("Can only use one type var tuple in a class def", base)
                            continue
                        have_type_var_tuple = True
                    tvars.append(tvar)
                elif not self.found_incomplete_ref(tag):
                    self.fail("Free type variable expected in %s[...]" % sym.node.name, base)
            return tvars, is_proto
        return None

    def analyze_unbound_tvar(self, t: Type) -> tuple[str, TypeVarLikeExpr] | None:
        if isinstance(t, UnpackType) and isinstance(t.type, UnboundType):
            return self.analyze_unbound_tvar_impl(t.type, is_unpacked=True)
        if isinstance(t, UnboundType):
            sym = self.lookup_qualified(t.name, t)
            if sym and sym.fullname in UNPACK_TYPE_NAMES:
                inner_t = t.args[0]
                if isinstance(inner_t, UnboundType):
                    return self.analyze_unbound_tvar_impl(inner_t, is_unpacked=True)
                return None
            return self.analyze_unbound_tvar_impl(t)
        return None

    def analyze_unbound_tvar_impl(
        self, t: UnboundType, is_unpacked: bool = False, is_typealias_param: bool = False
    ) -> tuple[str, TypeVarLikeExpr] | None:
        assert not is_unpacked or not is_typealias_param, "Mutually exclusive conditions"
        sym = self.lookup_qualified(t.name, t)
        if sym and isinstance(sym.node, PlaceholderNode):
            self.record_incomplete_ref()
        if not is_unpacked and sym and isinstance(sym.node, ParamSpecExpr):
            if sym.fullname and not self.tvar_scope.allow_binding(sym.fullname):
                # It's bound by our type variable scope
                return None
            return t.name, sym.node
        if (is_unpacked or is_typealias_param) and sym and isinstance(sym.node, TypeVarTupleExpr):
            if sym.fullname and not self.tvar_scope.allow_binding(sym.fullname):
                # It's bound by our type variable scope
                return None
            return t.name, sym.node
        if sym is None or not isinstance(sym.node, TypeVarExpr) or is_unpacked:
            return None
        elif sym.fullname and not self.tvar_scope.allow_binding(sym.fullname):
            # It's bound by our type variable scope
            return None
        else:
            assert isinstance(sym.node, TypeVarExpr)
            return t.name, sym.node

    def find_type_var_likes(self, t: Type) -> TypeVarLikeList:
        visitor = FindTypeVarVisitor(self, self.tvar_scope)
        t.accept(visitor)
        return visitor.type_var_likes

    def get_all_bases_tvars(
        self, base_type_exprs: list[Expression], removed: list[int]
    ) -> TypeVarLikeList:
        """Return all type variable references in bases."""
        tvars: TypeVarLikeList = []
        for i, base_expr in enumerate(base_type_exprs):
            if i not in removed:
                try:
                    base = self.expr_to_unanalyzed_type(base_expr)
                except TypeTranslationError:
                    # This error will be caught later.
                    continue
                base_tvars = self.find_type_var_likes(base)
                tvars.extend(base_tvars)
        return remove_dups(tvars)

    def tvar_defs_from_tvars(
        self, tvars: TypeVarLikeList, context: Context
    ) -> list[TypeVarLikeType]:
        tvar_defs: list[TypeVarLikeType] = []
        last_tvar_name_with_default: str | None = None
        for name, tvar_expr in tvars:
            tvar_expr.default = tvar_expr.default.accept(
                TypeVarDefaultTranslator(self, tvar_expr.name, context)
            )
            # PEP-695 type variables that are redeclared in an inner scope are warned
            # about elsewhere.
            if not tvar_expr.is_new_style and not self.tvar_scope.allow_binding(
                tvar_expr.fullname
            ):
                self.fail(
                    message_registry.TYPE_VAR_REDECLARED_IN_NESTED_CLASS.format(name), context
                )
            tvar_def = self.tvar_scope.bind_new(name, tvar_expr)
            if last_tvar_name_with_default is not None and not tvar_def.has_default():
                self.msg.tvar_without_default_type(
                    tvar_def.name, last_tvar_name_with_default, context
                )
                tvar_def.default = AnyType(TypeOfAny.from_error)
            elif tvar_def.has_default():
                last_tvar_name_with_default = tvar_def.name
            tvar_defs.append(tvar_def)
        return tvar_defs

    def get_and_bind_all_tvars(self, type_exprs: list[Expression]) -> list[TypeVarLikeType]:
        """Return all type variable references in item type expressions.

        This is a helper for generic TypedDicts and NamedTuples. Essentially it is
        a simplified version of the logic we use for ClassDef bases. We duplicate
        some amount of code, because it is hard to refactor common pieces.
        """
        tvars = []
        for base_expr in type_exprs:
            try:
                base = self.expr_to_unanalyzed_type(base_expr)
            except TypeTranslationError:
                # This error will be caught later.
                continue
            base_tvars = self.find_type_var_likes(base)
            tvars.extend(base_tvars)
        tvars = remove_dups(tvars)  # Variables are defined in order of textual appearance.
        tvar_defs = []
        for name, tvar_expr in tvars:
            tvar_def = self.tvar_scope.bind_new(name, tvar_expr)
            tvar_defs.append(tvar_def)
        return tvar_defs

    def prepare_class_def(
        self, defn: ClassDef, info: TypeInfo | None = None, custom_names: bool = False
    ) -> None:
        """Prepare for the analysis of a class definition.

        Create an empty TypeInfo and store it in a symbol table, or if the 'info'
        argument is provided, store it instead (used for magic type definitions).
        """
        if not defn.info:
            defn.fullname = self.qualified_name(defn.name)
            # TODO: Nested classes
            info = info or self.make_empty_type_info(defn)
            defn.info = info
            info.defn = defn
            if not custom_names:
                # Some special classes (in particular NamedTuples) use custom fullname logic.
                # Don't override it here (also see comment below, this needs cleanup).
                if not self.is_func_scope():
                    info._fullname = self.qualified_name(defn.name)
                else:
                    info._fullname = info.name
        local_name = defn.name
        if "@" in local_name:
            local_name = local_name.split("@")[0]
        self.add_symbol(local_name, defn.info, defn)
        if self.is_nested_within_func_scope():
            # We need to preserve local classes, let's store them
            # in globals under mangled unique names
            #
            # TODO: Putting local classes into globals breaks assumptions in fine-grained
            #       incremental mode and we should avoid it. In general, this logic is too
            #       ad-hoc and needs to be removed/refactored.
            if "@" not in defn.info._fullname:
                global_name = defn.info.name + "@" + str(defn.line)
                defn.info._fullname = self.cur_mod_id + "." + global_name
            else:
                # Preserve name from previous fine-grained incremental run.
                global_name = defn.info.name
            defn.fullname = defn.info._fullname
            if defn.info.is_named_tuple or defn.info.typeddict_type:
                # Named tuples and Typed dicts nested within a class are stored
                # in the class symbol table.
                self.add_symbol_skip_local(global_name, defn.info)
            else:
                self.globals[global_name] = SymbolTableNode(GDEF, defn.info)

    def make_empty_type_info(self, defn: ClassDef) -> TypeInfo:
        if (
            self.is_module_scope()
            and self.cur_mod_id == "builtins"
            and defn.name in CORE_BUILTIN_CLASSES
        ):
            # Special case core built-in classes. A TypeInfo was already
            # created for it before semantic analysis, but with a dummy
            # ClassDef. Patch the real ClassDef object.
            info = self.globals[defn.name].node
            assert isinstance(info, TypeInfo)
        else:
            info = TypeInfo(SymbolTable(), defn, self.cur_mod_id)
            info.set_line(defn)
        return info

    def get_name_repr_of_expr(self, expr: Expression) -> str | None:
        """Try finding a short simplified textual representation of a base class expression."""
        if isinstance(expr, NameExpr):
            return expr.name
        if isinstance(expr, MemberExpr):
            return get_member_expr_fullname(expr)
        if isinstance(expr, IndexExpr):
            return self.get_name_repr_of_expr(expr.base)
        if isinstance(expr, CallExpr):
            return self.get_name_repr_of_expr(expr.callee)
        return None

    def analyze_base_classes(
        self, base_type_exprs: list[Expression]
    ) -> tuple[list[tuple[ProperType, Expression]], bool] | None:
        """Analyze base class types.

        Return None if some definition was incomplete. Otherwise, return a tuple
        with these items:

         * List of (analyzed type, original expression) tuples
         * Boolean indicating whether one of the bases had a semantic analysis error
        """
        is_error = False
        bases = []
        for base_expr in base_type_exprs:
            if (
                isinstance(base_expr, RefExpr)
                and base_expr.fullname in TYPED_NAMEDTUPLE_NAMES + TPDICT_NAMES
            ) or (
                isinstance(base_expr, CallExpr)
                and isinstance(base_expr.callee, RefExpr)
                and base_expr.callee.fullname in TPDICT_NAMES
            ):
                # Ignore magic bases for now.
                # For example:
                #  class Foo(TypedDict): ...  # RefExpr
                #  class Foo(NamedTuple): ...  # RefExpr
                #  class Foo(TypedDict("Foo", {"a": int})): ...  # CallExpr
                continue

            try:
                base = self.expr_to_analyzed_type(
                    base_expr, allow_placeholder=True, allow_type_any=True
                )
            except TypeTranslationError:
                name = self.get_name_repr_of_expr(base_expr)
                if isinstance(base_expr, CallExpr):
                    msg = "Unsupported dynamic base class"
                else:
                    msg = "Invalid base class"
                if name:
                    msg += f' "{name}"'
                self.fail(msg, base_expr)
                is_error = True
                continue
            if base is None:
                return None
            base = get_proper_type(base)
            bases.append((base, base_expr))
        return bases, is_error

    def configure_base_classes(
        self, defn: ClassDef, bases: list[tuple[ProperType, Expression]]
    ) -> None:
        """Set up base classes.

        This computes several attributes on the corresponding TypeInfo defn.info
        related to the base classes: defn.info.bases, defn.info.mro, and
        miscellaneous others (at least tuple_type, fallback_to_any, and is_enum.)
        """
        base_types: list[Instance] = []
        info = defn.info

        for base, base_expr in bases:
            if isinstance(base, TupleType):
                actual_base = self.configure_tuple_base_class(defn, base)
                base_types.append(actual_base)
            elif isinstance(base, Instance):
                if base.type.is_newtype:
                    self.fail('Cannot subclass "NewType"', defn)
                base_types.append(base)
            elif isinstance(base, AnyType):
                if self.options.disallow_subclassing_any:
                    if isinstance(base_expr, (NameExpr, MemberExpr)):
                        msg = f'Class cannot subclass "{base_expr.name}" (has type "Any")'
                    else:
                        msg = 'Class cannot subclass value of type "Any"'
                    self.fail(msg, base_expr)
                info.fallback_to_any = True
            elif isinstance(base, TypedDictType):
                base_types.append(base.fallback)
            else:
                msg = "Invalid base class"
                name = self.get_name_repr_of_expr(base_expr)
                if name:
                    msg += f' "{name}"'
                self.fail(msg, base_expr)
                info.fallback_to_any = True
            if self.options.disallow_any_unimported and has_any_from_unimported_type(base):
                if isinstance(base_expr, (NameExpr, MemberExpr)):
                    prefix = f"Base type {base_expr.name}"
                else:
                    prefix = "Base type"
                self.msg.unimported_type_becomes_any(prefix, base, base_expr)
            check_for_explicit_any(
                base, self.options, self.is_typeshed_stub_file, self.msg, context=base_expr
            )

        # Add 'object' as implicit base if there is no other base class.
        if not base_types and defn.fullname != "builtins.object":
            base_types.append(self.object_type())

        info.bases = base_types

        # Calculate the MRO.
        if not self.verify_base_classes(defn):
            self.set_dummy_mro(defn.info)
            return
        if not self.verify_duplicate_base_classes(defn):
            # We don't want to block the typechecking process,
            # so, we just insert `Any` as the base class and show an error.
            self.set_any_mro(defn.info)
        self.calculate_class_mro(defn, self.object_type)

    def configure_tuple_base_class(self, defn: ClassDef, base: TupleType) -> Instance:
        info = defn.info

        # There may be an existing valid tuple type from previous semanal iterations.
        # Use equality to check if it is the case.
        if info.tuple_type and info.tuple_type != base and not has_placeholder(info.tuple_type):
            self.fail("Class has two incompatible bases derived from tuple", defn)
            defn.has_incompatible_baseclass = True
        if info.special_alias and has_placeholder(info.special_alias.target):
            self.process_placeholder(
                None, "tuple base", defn, force_progress=base != info.tuple_type
            )
        info.update_tuple_type(base)
        self.setup_alias_type_vars(defn)

        if base.partial_fallback.type.fullname == "builtins.tuple" and not has_placeholder(base):
            # Fallback can only be safely calculated after semantic analysis, since base
            # classes may be incomplete. Postpone the calculation.
            self.schedule_patch(PRIORITY_FALLBACKS, lambda: calculate_tuple_fallback(base))

        return base.partial_fallback

    def set_dummy_mro(self, info: TypeInfo) -> None:
        # Give it an MRO consisting of just the class itself and object.
        info.mro = [info, self.object_type().type]
        info.bad_mro = True

    def set_any_mro(self, info: TypeInfo) -> None:
        # Give it an MRO consisting direct `Any` subclass.
        info.fallback_to_any = True
        info.mro = [info, self.object_type().type]

    def calculate_class_mro(
        self, defn: ClassDef, obj_type: Callable[[], Instance] | None = None
    ) -> None:
        """Calculate method resolution order for a class.

        `obj_type` exists just to fill in empty base class list in case of an error.
        """
        try:
            calculate_mro(defn.info, obj_type)
        except MroError:
            self.fail(
                f'Cannot determine consistent method resolution order (MRO) for "{defn.name}"',
                defn,
            )
            self.set_dummy_mro(defn.info)
        # Allow plugins to alter the MRO to handle the fact that `def mro()`
        # on metaclasses permits MRO rewriting.
        if defn.fullname:
            hook = self.plugin.get_customize_class_mro_hook(defn.fullname)
            if hook:
                hook(ClassDefContext(defn, FakeExpression(), self))

    def infer_metaclass_and_bases_from_compat_helpers(self, defn: ClassDef) -> None:
        """Lookup for special metaclass declarations, and update defn fields accordingly.

        * six.with_metaclass(M, B1, B2, ...)
        * @six.add_metaclass(M)
        * future.utils.with_metaclass(M, B1, B2, ...)
        * past.utils.with_metaclass(M, B1, B2, ...)
        """

        # Look for six.with_metaclass(M, B1, B2, ...)
        with_meta_expr: Expression | None = None
        if len(defn.base_type_exprs) == 1:
            base_expr = defn.base_type_exprs[0]
            if isinstance(base_expr, CallExpr) and isinstance(base_expr.callee, RefExpr):
                self.analyze_type_expr(base_expr)
                if (
                    base_expr.callee.fullname
                    in {
                        "six.with_metaclass",
                        "future.utils.with_metaclass",
                        "past.utils.with_metaclass",
                    }
                    and len(base_expr.args) >= 1
                    and all(kind == ARG_POS for kind in base_expr.arg_kinds)
                ):
                    with_meta_expr = base_expr.args[0]
                    defn.base_type_exprs = base_expr.args[1:]

        # Look for @six.add_metaclass(M)
        add_meta_expr: Expression | None = None
        for dec_expr in defn.decorators:
            if isinstance(dec_expr, CallExpr) and isinstance(dec_expr.callee, RefExpr):
                dec_expr.callee.accept(self)
                if (
                    dec_expr.callee.fullname == "six.add_metaclass"
                    and len(dec_expr.args) == 1
                    and dec_expr.arg_kinds[0] == ARG_POS
                ):
                    add_meta_expr = dec_expr.args[0]
                    break

        metas = {defn.metaclass, with_meta_expr, add_meta_expr} - {None}
        if len(metas) == 0:
            return
        if len(metas) > 1:
            self.fail("Multiple metaclass definitions", defn, code=codes.METACLASS)
            return
        defn.metaclass = metas.pop()

    def verify_base_classes(self, defn: ClassDef) -> bool:
        info = defn.info
        cycle = False
        for base in info.bases:
            baseinfo = base.type
            if self.is_base_class(info, baseinfo):
                self.fail("Cycle in inheritance hierarchy", defn)
                cycle = True
        return not cycle

    def verify_duplicate_base_classes(self, defn: ClassDef) -> bool:
        dup = find_duplicate(defn.info.direct_base_classes())
        if dup:
            self.fail(f'Duplicate base class "{dup.name}"', defn)
        return not dup

    def is_base_class(self, t: TypeInfo, s: TypeInfo) -> bool:
        """Determine if t is a base class of s (but do not use mro)."""
        # Search the base class graph for t, starting from s.
        worklist = [s]
        visited = {s}
        while worklist:
            nxt = worklist.pop()
            if nxt == t:
                return True
            for base in nxt.bases:
                if base.type not in visited:
                    worklist.append(base.type)
                    visited.add(base.type)
        return False

    def get_declared_metaclass(
        self, name: str, metaclass_expr: Expression | None
    ) -> tuple[Instance | None, bool, bool]:
        """Get declared metaclass from metaclass expression.

        Returns a tuple of three values:
          * A metaclass instance or None
          * A boolean indicating whether we should defer
          * A boolean indicating whether we should set metaclass Any fallback
            (either for Any metaclass or invalid/dynamic metaclass).

        The two boolean flags can only be True if instance is None.
        """
        declared_metaclass = None
        if metaclass_expr:
            metaclass_name = None
            if isinstance(metaclass_expr, NameExpr):
                metaclass_name = metaclass_expr.name
            elif isinstance(metaclass_expr, MemberExpr):
                metaclass_name = get_member_expr_fullname(metaclass_expr)
            if metaclass_name is None:
                self.fail(
                    f'Dynamic metaclass not supported for "{name}"',
                    metaclass_expr,
                    code=codes.METACLASS,
                )
                return None, False, True
            sym = self.lookup_qualified(metaclass_name, metaclass_expr)
            if sym is None:
                # Probably a name error - it is already handled elsewhere
                return None, False, True
            if isinstance(sym.node, Var) and isinstance(get_proper_type(sym.node.type), AnyType):
                if self.options.disallow_subclassing_any:
                    self.fail(
                        f'Class cannot use "{sym.node.name}" as a metaclass (has type "Any")',
                        metaclass_expr,
                        code=codes.METACLASS,
                    )
                return None, False, True
            if isinstance(sym.node, PlaceholderNode):
                return None, True, False  # defer later in the caller

            # Support type aliases, like `_Meta: TypeAlias = type`
            metaclass_info: Node | None = sym.node
            if (
                isinstance(sym.node, TypeAlias)
                and not sym.node.python_3_12_type_alias
                and not sym.node.alias_tvars
            ):
                target = get_proper_type(sym.node.target)
                if isinstance(target, Instance):
                    metaclass_info = target.type

            if not isinstance(metaclass_info, TypeInfo) or metaclass_info.tuple_type is not None:
                self.fail(
                    f'Invalid metaclass "{metaclass_name}"', metaclass_expr, code=codes.METACLASS
                )
                return None, False, False
            if not metaclass_info.is_metaclass():
                self.fail(
                    'Metaclasses not inheriting from "type" are not supported',
                    metaclass_expr,
                    code=codes.METACLASS,
                )
                return None, False, False
            inst = fill_typevars(metaclass_info)
            assert isinstance(inst, Instance)
            declared_metaclass = inst
        return declared_metaclass, False, False

    def recalculate_metaclass(self, defn: ClassDef, declared_metaclass: Instance | None) -> None:
        defn.info.declared_metaclass = declared_metaclass
        defn.info.metaclass_type = defn.info.calculate_metaclass_type()
        if any(info.is_protocol for info in defn.info.mro):
            if (
                not defn.info.metaclass_type
                or defn.info.metaclass_type.type.fullname == "builtins.type"
            ):
                # All protocols and their subclasses have ABCMeta metaclass by default.
                # TODO: add a metaclass conflict check if there is another metaclass.
                abc_meta = self.named_type_or_none("abc.ABCMeta", [])
                if abc_meta is not None:  # May be None in tests with incomplete lib-stub.
                    defn.info.metaclass_type = abc_meta
        if defn.info.metaclass_type and defn.info.metaclass_type.type.has_base("enum.EnumMeta"):
            defn.info.is_enum = True
            if defn.type_vars:
                self.fail("Enum class cannot be generic", defn)

    #
    # Imports
    #

    def visit_import(self, i: Import) -> None:
        self.statement = i
        for id, as_id in i.ids:
            # Modules imported in a stub file without using 'import X as X' won't get exported
            # When implicit re-exporting is disabled, we have the same behavior as stubs.
            use_implicit_reexport = not self.is_stub_file and self.options.implicit_reexport
            if as_id is not None:
                base_id = id
                imported_id = as_id
                module_public = use_implicit_reexport or id == as_id
            else:
                base_id = id.split(".")[0]
                imported_id = base_id
                module_public = use_implicit_reexport

            if base_id in self.modules:
                node = self.modules[base_id]
                if self.is_func_scope():
                    kind = LDEF
                elif self.type is not None:
                    kind = MDEF
                else:
                    kind = GDEF
                symbol = SymbolTableNode(
                    kind, node, module_public=module_public, module_hidden=not module_public
                )
                self.add_imported_symbol(
                    imported_id,
                    symbol,
                    context=i,
                    module_public=module_public,
                    module_hidden=not module_public,
                )
            else:
                self.add_unknown_imported_symbol(
                    imported_id,
                    context=i,
                    target_name=base_id,
                    module_public=module_public,
                    module_hidden=not module_public,
                )

    def visit_import_from(self, imp: ImportFrom) -> None:
        self.statement = imp
        module_id = self.correct_relative_import(imp)
        module = self.modules.get(module_id)
        for id, as_id in imp.names:
            fullname = module_id + "." + id
            self.set_future_import_flags(fullname)
            if module is None:
                node = None
            elif module_id == self.cur_mod_id and fullname in self.modules:
                # Submodule takes precedence over definition in surround package, for
                # compatibility with runtime semantics in typical use cases. This
                # could more precisely model runtime semantics by taking into account
                # the line number beyond which the local definition should take
                # precedence, but doesn't seem to be important in most use cases.
                node = SymbolTableNode(GDEF, self.modules[fullname])
            else:
                if id == as_id == "__all__" and module_id in self.export_map:
                    self.all_exports[:] = self.export_map[module_id]
                node = module.names.get(id)

            missing_submodule = False
            imported_id = as_id or id

            # Modules imported in a stub file without using 'from Y import X as X' will
            # not get exported.
            # When implicit re-exporting is disabled, we have the same behavior as stubs.
            use_implicit_reexport = not self.is_stub_file and self.options.implicit_reexport
            module_public = use_implicit_reexport or (as_id is not None and id == as_id)

            # If the module does not contain a symbol with the name 'id',
            # try checking if it's a module instead.
            if not node:
                mod = self.modules.get(fullname)
                if mod is not None:
                    kind = self.current_symbol_kind()
                    node = SymbolTableNode(kind, mod)
                elif fullname in self.missing_modules:
                    missing_submodule = True
            # If it is still not resolved, check for a module level __getattr__
            if module and not node and "__getattr__" in module.names:
                # We store the fullname of the original definition so that we can
                # detect whether two imported names refer to the same thing.
                fullname = module_id + "." + id
                gvar = self.create_getattr_var(module.names["__getattr__"], imported_id, fullname)
                if gvar:
                    self.add_symbol(
                        imported_id,
                        gvar,
                        imp,
                        module_public=module_public,
                        module_hidden=not module_public,
                    )
                    continue

            if node:
                self.process_imported_symbol(
                    node, module_id, id, imported_id, fullname, module_public, context=imp
                )
                if node.module_hidden:
                    self.report_missing_module_attribute(
                        module_id,
                        id,
                        imported_id,
                        module_public=module_public,
                        module_hidden=not module_public,
                        context=imp,
                        add_unknown_imported_symbol=False,
                    )
            elif module and not missing_submodule:
                # Target module exists but the imported name is missing or hidden.
                self.report_missing_module_attribute(
                    module_id,
                    id,
                    imported_id,
                    module_public=module_public,
                    module_hidden=not module_public,
                    context=imp,
                )
            else:
                # Import of a missing (sub)module.
                self.add_unknown_imported_symbol(
                    imported_id,
                    imp,
                    target_name=fullname,
                    module_public=module_public,
                    module_hidden=not module_public,
                )

    def process_imported_symbol(
        self,
        node: SymbolTableNode,
        module_id: str,
        id: str,
        imported_id: str,
        fullname: str,
        module_public: bool,
        context: ImportBase,
    ) -> None:
        module_hidden = not module_public and (
            # `from package import submodule` should work regardless of whether package
            # re-exports submodule, so we shouldn't hide it
            not isinstance(node.node, MypyFile)
            or fullname not in self.modules
            # but given `from somewhere import random_unrelated_module` we should hide
            # random_unrelated_module
            or not fullname.startswith(self.cur_mod_id + ".")
        )

        if isinstance(node.node, PlaceholderNode):
            if self.final_iteration:
                self.report_missing_module_attribute(
                    module_id,
                    id,
                    imported_id,
                    module_public=module_public,
                    module_hidden=module_hidden,
                    context=context,
                )
                return
            else:
                # This might become a type.
                self.mark_incomplete(
                    imported_id,
                    node.node,
                    module_public=module_public,
                    module_hidden=module_hidden,
                    becomes_typeinfo=True,
                )
        # NOTE: we take the original node even for final `Var`s. This is to support
        # a common pattern when constants are re-exported (same applies to import *).
        self.add_imported_symbol(
            imported_id, node, context, module_public=module_public, module_hidden=module_hidden
        )

    def report_missing_module_attribute(
        self,
        import_id: str,
        source_id: str,
        imported_id: str,
        module_public: bool,
        module_hidden: bool,
        context: Node,
        add_unknown_imported_symbol: bool = True,
    ) -> None:
        # Missing attribute.
        if self.is_incomplete_namespace(import_id):
            # We don't know whether the name will be there, since the namespace
            # is incomplete. Defer the current target.
            self.mark_incomplete(
                imported_id, context, module_public=module_public, module_hidden=module_hidden
            )
            return
        message = f'Module "{import_id}" has no attribute "{source_id}"'
        # Suggest alternatives, if any match is found.
        module = self.modules.get(import_id)
        if module:
            if source_id in module.names.keys() and not module.names[source_id].module_public:
                message = (
                    f'Module "{import_id}" does not explicitly export attribute "{source_id}"'
                )
            else:
                alternatives = set(module.names.keys()).difference({source_id})
                matches = best_matches(source_id, alternatives, n=3)
                if matches:
                    suggestion = f"; maybe {pretty_seq(matches, 'or')}?"
                    message += f"{suggestion}"
        self.fail(message, context, code=codes.ATTR_DEFINED)
        if add_unknown_imported_symbol:
            self.add_unknown_imported_symbol(
                imported_id,
                context,
                target_name=None,
                module_public=module_public,
                module_hidden=not module_public,
            )

        if import_id == "typing":
            # The user probably has a missing definition in a test fixture. Let's verify.
            fullname = f"builtins.{source_id.lower()}"
            if (
                self.lookup_fully_qualified_or_none(fullname) is None
                and fullname in SUGGESTED_TEST_FIXTURES
            ):
                # Yes. Generate a helpful note.
                self.msg.add_fixture_note(fullname, context)
            else:
                typing_extensions = self.modules.get("typing_extensions")
                if typing_extensions and source_id in typing_extensions.names:
                    self.msg.note(
                        f"Use `from typing_extensions import {source_id}` instead",
                        context,
                        code=codes.ATTR_DEFINED,
                    )
                    self.msg.note(
                        "See https://mypy.readthedocs.io/en/stable/runtime_troubles.html#using-new-additions-to-the-typing-module",
                        context,
                        code=codes.ATTR_DEFINED,
                    )

    def process_import_over_existing_name(
        self,
        imported_id: str,
        existing_symbol: SymbolTableNode,
        module_symbol: SymbolTableNode,
        import_node: ImportBase,
    ) -> bool:
        if existing_symbol.node is module_symbol.node:
            # We added this symbol on previous iteration.
            return False
        if existing_symbol.kind in (LDEF, GDEF, MDEF) and isinstance(
            existing_symbol.node, (Var, FuncDef, TypeInfo, Decorator, TypeAlias)
        ):
            # This is a valid import over an existing definition in the file. Construct a dummy
            # assignment that we'll use to type check the import.
            lvalue = NameExpr(imported_id)
            lvalue.kind = existing_symbol.kind
            lvalue.node = existing_symbol.node
            rvalue = NameExpr(imported_id)
            rvalue.kind = module_symbol.kind
            rvalue.node = module_symbol.node
            if isinstance(rvalue.node, TypeAlias):
                # Suppress bogus errors from the dummy assignment if rvalue is an alias.
                # Otherwise mypy may complain that alias is invalid in runtime context.
                rvalue.is_alias_rvalue = True
            assignment = AssignmentStmt([lvalue], rvalue)
            for node in assignment, lvalue, rvalue:
                node.set_line(import_node)
            import_node.assignments.append(assignment)
            return True
        return False

    def correct_relative_import(self, node: ImportFrom | ImportAll) -> str:
        import_id, ok = correct_relative_import(
            self.cur_mod_id, node.relative, node.id, self.cur_mod_node.is_package_init_file()
        )
        if not ok:
            self.fail("Relative import climbs too many namespaces", node)
        return import_id

    def visit_import_all(self, i: ImportAll) -> None:
        i_id = self.correct_relative_import(i)
        if i_id in self.modules:
            m = self.modules[i_id]
            if self.is_incomplete_namespace(i_id):
                # Any names could be missing from the current namespace if the target module
                # namespace is incomplete.
                self.mark_incomplete("*", i)
            for name, node in m.names.items():
                fullname = i_id + "." + name
                self.set_future_import_flags(fullname)
                # if '__all__' exists, all nodes not included have had module_public set to
                # False, and we can skip checking '_' because it's been explicitly included.
                if node.module_public and (not name.startswith("_") or "__all__" in m.names):
                    if isinstance(node.node, MypyFile):
                        # Star import of submodule from a package, add it as a dependency.
                        self.imports.add(node.node.fullname)
                    # `from x import *` always reexports symbols
                    self.add_imported_symbol(
                        name, node, context=i, module_public=True, module_hidden=False
                    )

        else:
            # Don't add any dummy symbols for 'from x import *' if 'x' is unknown.
            pass

    #
    # Assignment
    #

    def visit_assignment_expr(self, s: AssignmentExpr) -> None:
        s.value.accept(self)
        if self.is_func_scope():
            if not self.check_valid_comprehension(s):
                return
        self.analyze_lvalue(s.target, escape_comprehensions=True, has_explicit_value=True)

    def check_valid_comprehension(self, s: AssignmentExpr) -> bool:
        """Check that assignment expression is not nested within comprehension at class scope.

        class C:
            [(j := i) for i in [1, 2, 3]]
        is a syntax error that is not enforced by Python parser, but at later steps.
        """
        for i, scope_type in enumerate(reversed(self.scope_stack)):
            if scope_type != SCOPE_COMPREHENSION and i < len(self.locals) - 1:
                if self.locals[-1 - i] is None:
                    self.fail(
                        "Assignment expression within a comprehension"
                        " cannot be used in a class body",
                        s,
                        code=codes.SYNTAX,
                        serious=True,
                        blocker=True,
                    )
                    return False
                break
        return True

    def visit_assignment_stmt(self, s: AssignmentStmt) -> None:
        self.statement = s

        # Special case assignment like X = X.
        if self.analyze_identity_global_assignment(s):
            return

        tag = self.track_incomplete_refs()

        # Here we have a chicken and egg problem: at this stage we can't call
        # can_be_type_alias(), because we have not enough information about rvalue.
        # But we can't use a full visit because it may emit extra incomplete refs (namely
        # when analysing any type applications there) thus preventing the further analysis.
        # To break the tie, we first analyse rvalue partially, if it can be a type alias.
        if self.can_possibly_be_type_form(s):
            old_basic_type_applications = self.basic_type_applications
            self.basic_type_applications = True
            with self.allow_unbound_tvars_set():
                s.rvalue.accept(self)
            self.basic_type_applications = old_basic_type_applications
        elif self.can_possibly_be_typevarlike_declaration(s):
            # Allow unbound tvars inside TypeVarLike defaults to be evaluated later
            with self.allow_unbound_tvars_set():
                s.rvalue.accept(self)
        else:
            s.rvalue.accept(self)

        if self.found_incomplete_ref(tag) or self.should_wait_rhs(s.rvalue):
            # Initializer couldn't be fully analyzed. Defer the current node and give up.
            # Make sure that if we skip the definition of some local names, they can't be
            # added later in this scope, since an earlier definition should take precedence.
            for expr in names_modified_by_assignment(s):
                self.mark_incomplete(expr.name, expr)
            return
        if self.can_possibly_be_type_form(s):
            # Now re-visit those rvalues that were we skipped type applications above.
            # This should be safe as generally semantic analyzer is idempotent.
            with self.allow_unbound_tvars_set():
                s.rvalue.accept(self)

        # The r.h.s. is now ready to be classified, first check if it is a special form:
        special_form = False
        # * type alias
        if self.check_and_set_up_type_alias(s):
            s.is_alias_def = True
            special_form = True
        elif isinstance(s.rvalue, CallExpr):
            # * type variable definition
            if self.process_typevar_declaration(s):
                special_form = True
            elif self.process_paramspec_declaration(s):
                special_form = True
            elif self.process_typevartuple_declaration(s):
                special_form = True
            # * type constructors
            elif self.analyze_namedtuple_assign(s):
                special_form = True
            elif self.analyze_typeddict_assign(s):
                special_form = True
            elif self.newtype_analyzer.process_newtype_declaration(s):
                special_form = True
            elif self.analyze_enum_assign(s):
                special_form = True

        if special_form:
            self.record_special_form_lvalue(s)
            return
        # Clear the alias flag if assignment turns out not a special form after all. It
        # may be set to True while there were still placeholders due to forward refs.
        s.is_alias_def = False

        # OK, this is a regular assignment, perform the necessary analysis steps.
        s.is_final_def = self.unwrap_final(s)
        self.analyze_lvalues(s)
        self.check_final_implicit_def(s)
        self.store_final_status(s)
        self.check_classvar(s)
        self.process_type_annotation(s)
        self.apply_dynamic_class_hook(s)
        if not s.type:
            self.process_module_assignment(s.lvalues, s.rvalue, s)
        self.process__all__(s)
        self.process__deletable__(s)
        self.process__slots__(s)

    def analyze_identity_global_assignment(self, s: AssignmentStmt) -> bool:
        """Special case 'X = X' in global scope.

        This allows supporting some important use cases.

        Return true if special casing was applied.
        """
        if not isinstance(s.rvalue, NameExpr) or len(s.lvalues) != 1:
            # Not of form 'X = X'
            return False
        lvalue = s.lvalues[0]
        if not isinstance(lvalue, NameExpr) or s.rvalue.name != lvalue.name:
            # Not of form 'X = X'
            return False
        if self.type is not None or self.is_func_scope():
            # Not in global scope
            return False
        # It's an assignment like 'X = X' in the global scope.
        name = lvalue.name
        sym = self.lookup(name, s)
        if sym is None:
            if self.final_iteration:
                # Fall back to normal assignment analysis.
                return False
            else:
                self.defer()
                return True
        else:
            if sym.node is None:
                # Something special -- fall back to normal assignment analysis.
                return False
            if name not in self.globals:
                # The name is from builtins. Add an alias to the current module.
                self.add_symbol(name, sym.node, s)
            if not isinstance(sym.node, PlaceholderNode):
                for node in s.rvalue, lvalue:
                    node.node = sym.node
                    node.kind = GDEF
                    node.fullname = sym.node.fullname
            return True

    def should_wait_rhs(self, rv: Expression) -> bool:
        """Can we already classify this r.h.s. of an assignment or should we wait?

        This returns True if we don't have enough information to decide whether
        an assignment is just a normal variable definition or a special form.
        Always return False if this is a final iteration. This will typically cause
        the lvalue to be classified as a variable plus emit an error.
        """
        if self.final_iteration:
            # No chance, nothing has changed.
            return False
        if isinstance(rv, NameExpr):
            n = self.lookup(rv.name, rv)
            if n and isinstance(n.node, PlaceholderNode) and not n.node.becomes_typeinfo:
                return True
        elif isinstance(rv, MemberExpr):
            fname = get_member_expr_fullname(rv)
            if fname:
                n = self.lookup_qualified(fname, rv, suppress_errors=True)
                if n and isinstance(n.node, PlaceholderNode) and not n.node.becomes_typeinfo:
                    return True
        elif isinstance(rv, IndexExpr) and isinstance(rv.base, RefExpr):
            return self.should_wait_rhs(rv.base)
        elif isinstance(rv, CallExpr) and isinstance(rv.callee, RefExpr):
            # This is only relevant for builtin SCC where things like 'TypeVar'
            # may be not ready.
            return self.should_wait_rhs(rv.callee)
        return False

    def can_be_type_alias(self, rv: Expression, allow_none: bool = False) -> bool:
        """Is this a valid r.h.s. for an alias definition?

        Note: this function should be only called for expressions where self.should_wait_rhs()
        returns False.
        """
        if isinstance(rv, RefExpr) and self.is_type_ref(rv, bare=True):
            return True
        if isinstance(rv, IndexExpr) and self.is_type_ref(rv.base, bare=False):
            return True
        if self.is_none_alias(rv):
            return True
        if allow_none and isinstance(rv, NameExpr) and rv.fullname == "builtins.None":
            return True
        if isinstance(rv, OpExpr) and rv.op == "|":
            if self.is_stub_file:
                return True
            if self.can_be_type_alias(rv.left, allow_none=True) and self.can_be_type_alias(
                rv.right, allow_none=True
            ):
                return True
        return False

    def can_possibly_be_type_form(self, s: AssignmentStmt) -> bool:
        """Like can_be_type_alias(), but simpler and doesn't require fully analyzed rvalue.

        Instead, use lvalues/annotations structure to figure out whether this can potentially be
        a type alias definition, NamedTuple, or TypedDict. Another difference from above function
        is that we are only interested IndexExpr, CallExpr and OpExpr rvalues, since only those
        can be potentially recursive (things like `A = A` are never valid).
        """
        if len(s.lvalues) > 1:
            return False
        if isinstance(s.rvalue, CallExpr) and isinstance(s.rvalue.callee, RefExpr):
            ref = s.rvalue.callee.fullname
            return ref in TPDICT_NAMES or ref in TYPED_NAMEDTUPLE_NAMES
        if not isinstance(s.lvalues[0], NameExpr):
            return False
        if s.unanalyzed_type is not None and not self.is_pep_613(s):
            return False
        if not isinstance(s.rvalue, (IndexExpr, OpExpr)):
            return False
        # Something that looks like Foo = Bar[Baz, ...]
        return True

    def can_possibly_be_typevarlike_declaration(self, s: AssignmentStmt) -> bool:
        """Check if r.h.s. can be a TypeVarLike declaration."""
        if len(s.lvalues) != 1 or not isinstance(s.lvalues[0], NameExpr):
            return False
        if not isinstance(s.rvalue, CallExpr) or not isinstance(s.rvalue.callee, NameExpr):
            return False
        ref = s.rvalue.callee
        ref.accept(self)
        return ref.fullname in TYPE_VAR_LIKE_NAMES

    def is_type_ref(self, rv: Expression, bare: bool = False) -> bool:
        """Does this expression refer to a type?

        This includes:
          * Special forms, like Any or Union
          * Classes (except subscripted enums)
          * Other type aliases
          * PlaceholderNodes with becomes_typeinfo=True (these can be not ready class
            definitions, and not ready aliases).

        If bare is True, this is not a base of an index expression, so some special
        forms are not valid (like a bare Union).

        Note: This method should be only used in context of a type alias definition.
        This method can only return True for RefExprs, to check if C[int] is a valid
        target for type alias call this method on expr.base (i.e. on C in C[int]).
        See also can_be_type_alias().
        """
        if not isinstance(rv, RefExpr):
            return False
        if isinstance(rv.node, TypeVarLikeExpr):
            self.fail(f'Type variable "{rv.fullname}" is invalid as target for type alias', rv)
            return False

        if bare:
            # These three are valid even if bare, for example
            # A = Tuple is just equivalent to A = Tuple[Any, ...].
            valid_refs = {"typing.Any", "typing.Tuple", "typing.Callable"}
        else:
            valid_refs = type_constructors

        if isinstance(rv.node, TypeAlias) or rv.fullname in valid_refs:
            return True
        if isinstance(rv.node, TypeInfo):
            if bare:
                return True
            # Assignment color = Color['RED'] defines a variable, not an alias.
            return not rv.node.is_enum
        if isinstance(rv.node, Var):
            return rv.node.fullname in NEVER_NAMES

        if isinstance(rv, NameExpr):
            n = self.lookup(rv.name, rv)
            if n and isinstance(n.node, PlaceholderNode) and n.node.becomes_typeinfo:
                return True
        elif isinstance(rv, MemberExpr):
            fname = get_member_expr_fullname(rv)
            if fname:
                # The r.h.s. for variable definitions may not be a type reference but just
                # an instance attribute, so suppress the errors.
                n = self.lookup_qualified(fname, rv, suppress_errors=True)
                if n and isinstance(n.node, PlaceholderNode) and n.node.becomes_typeinfo:
                    return True
        return False

    def is_none_alias(self, node: Expression) -> bool:
        """Is this a r.h.s. for a None alias?

        We special case the assignments like Void = type(None), to allow using
        Void in type annotations.
        """
        if isinstance(node, CallExpr):
            if (
                isinstance(node.callee, NameExpr)
                and len(node.args) == 1
                and isinstance(node.args[0], NameExpr)
            ):
                call = self.lookup_qualified(node.callee.name, node.callee)
                arg = self.lookup_qualified(node.args[0].name, node.args[0])
                if (
                    call is not None
                    and call.node
                    and call.node.fullname == "builtins.type"
                    and arg is not None
                    and arg.node
                    and arg.node.fullname == "builtins.None"
                ):
                    return True
        return False

    def record_special_form_lvalue(self, s: AssignmentStmt) -> None:
        """Record minimal necessary information about l.h.s. of a special form.

        This exists mostly for compatibility with the old semantic analyzer.
        """
        lvalue = s.lvalues[0]
        assert isinstance(lvalue, NameExpr)
        lvalue.is_special_form = True
        if self.current_symbol_kind() == GDEF:
            lvalue.fullname = self.qualified_name(lvalue.name)
        lvalue.kind = self.current_symbol_kind()

    def analyze_enum_assign(self, s: AssignmentStmt) -> bool:
        """Check if s defines an Enum."""
        if isinstance(s.rvalue, CallExpr) and isinstance(s.rvalue.analyzed, EnumCallExpr):
            # This is an analyzed enum definition.
            # It is valid iff it can be stored correctly, failures were already reported.
            return self._is_single_name_assignment(s)
        return self.enum_call_analyzer.process_enum_call(s, self.is_func_scope())

    def analyze_namedtuple_assign(self, s: AssignmentStmt) -> bool:
        """Check if s defines a namedtuple."""
        if isinstance(s.rvalue, CallExpr) and isinstance(s.rvalue.analyzed, NamedTupleExpr):
            if s.rvalue.analyzed.info.tuple_type and not has_placeholder(
                s.rvalue.analyzed.info.tuple_type
            ):
                # This is an analyzed named tuple definition.
                # It is valid iff it can be stored correctly, failures were already reported.
                return self._is_single_name_assignment(s)
        if len(s.lvalues) != 1 or not isinstance(s.lvalues[0], (NameExpr, MemberExpr)):
            return False
        lvalue = s.lvalues[0]
        if isinstance(lvalue, MemberExpr):
            if isinstance(s.rvalue, CallExpr) and isinstance(s.rvalue.callee, RefExpr):
                fullname = s.rvalue.callee.fullname
                if fullname == "collections.namedtuple" or fullname in TYPED_NAMEDTUPLE_NAMES:
                    self.fail("NamedTuple type as an attribute is not supported", lvalue)
            return False
        name = lvalue.name
        namespace = self.qualified_name(name)
        with self.tvar_scope_frame(self.tvar_scope.class_frame(namespace)):
            internal_name, info, tvar_defs = self.named_tuple_analyzer.check_namedtuple(
                s.rvalue, name, self.is_func_scope()
            )
            if internal_name is None:
                return False
            if internal_name != name:
                self.fail(
                    'First argument to namedtuple() should be "{}", not "{}"'.format(
                        name, internal_name
                    ),
                    s.rvalue,
                    code=codes.NAME_MATCH,
                )
                return True
            # Yes, it's a valid namedtuple, but defer if it is not ready.
            if not info:
                self.mark_incomplete(name, lvalue, becomes_typeinfo=True)
            else:
                self.setup_type_vars(info.defn, tvar_defs)
                self.setup_alias_type_vars(info.defn)
            return True

    def analyze_typeddict_assign(self, s: AssignmentStmt) -> bool:
        """Check if s defines a typed dict."""
        if isinstance(s.rvalue, CallExpr) and isinstance(s.rvalue.analyzed, TypedDictExpr):
            if s.rvalue.analyzed.info.typeddict_type and not has_placeholder(
                s.rvalue.analyzed.info.typeddict_type
            ):
                # This is an analyzed typed dict definition.
                # It is valid iff it can be stored correctly, failures were already reported.
                return self._is_single_name_assignment(s)
        if len(s.lvalues) != 1 or not isinstance(s.lvalues[0], (NameExpr, MemberExpr)):
            return False
        lvalue = s.lvalues[0]
        name = lvalue.name
        namespace = self.qualified_name(name)
        with self.tvar_scope_frame(self.tvar_scope.class_frame(namespace)):
            is_typed_dict, info, tvar_defs = self.typed_dict_analyzer.check_typeddict(
                s.rvalue, name, self.is_func_scope()
            )
            if not is_typed_dict:
                return False
            if isinstance(lvalue, MemberExpr):
                self.fail("TypedDict type as attribute is not supported", lvalue)
                return False
            # Yes, it's a valid typed dict, but defer if it is not ready.
            if not info:
                self.mark_incomplete(name, lvalue, becomes_typeinfo=True)
            else:
                defn = info.defn
                self.setup_type_vars(defn, tvar_defs)
                self.setup_alias_type_vars(defn)
            return True

    def _is_single_name_assignment(self, s: AssignmentStmt) -> bool:
        return len(s.lvalues) == 1 and isinstance(s.lvalues[0], NameExpr)

    def analyze_lvalues(self, s: AssignmentStmt) -> None:
        # We cannot use s.type, because analyze_simple_literal_type() will set it.
        explicit = s.unanalyzed_type is not None
        if self.is_final_type(s.unanalyzed_type):
            # We need to exclude bare Final.
            assert isinstance(s.unanalyzed_type, UnboundType)
            if not s.unanalyzed_type.args:
                explicit = False

        if s.rvalue:
            if isinstance(s.rvalue, TempNode):
                has_explicit_value = not s.rvalue.no_rhs
            else:
                has_explicit_value = True
        else:
            has_explicit_value = False

        for lval in s.lvalues:
            self.analyze_lvalue(
                lval,
                explicit_type=explicit,
                is_final=s.is_final_def,
                has_explicit_value=has_explicit_value,
            )

    def apply_dynamic_class_hook(self, s: AssignmentStmt) -> None:
        if not isinstance(s.rvalue, CallExpr):
            return
        fname = ""
        call = s.rvalue
        while True:
            if isinstance(call.callee, RefExpr):
                fname = call.callee.fullname
            # check if method call
            if not fname and isinstance(call.callee, MemberExpr):
                callee_expr = call.callee.expr
                if isinstance(callee_expr, RefExpr) and callee_expr.fullname:
                    method_name = call.callee.name
                    fname = callee_expr.fullname + "." + method_name
                elif (
                    isinstance(callee_expr, IndexExpr)
                    and isinstance(callee_expr.base, RefExpr)
                    and isinstance(callee_expr.analyzed, TypeApplication)
                ):
                    method_name = call.callee.name
                    fname = callee_expr.base.fullname + "." + method_name
                elif isinstance(callee_expr, CallExpr):
                    # check if chain call
                    call = callee_expr
                    continue
            break
        if not fname:
            return
        hook = self.plugin.get_dynamic_class_hook(fname)
        if not hook:
            return
        for lval in s.lvalues:
            if not isinstance(lval, NameExpr):
                continue
            hook(DynamicClassDefContext(call, lval.name, self))

    def unwrap_final(self, s: AssignmentStmt) -> bool:
        """Strip Final[...] if present in an assignment.

        This is done to invoke type inference during type checking phase for this
        assignment. Also, Final[...] doesn't affect type in any way -- it is rather an
        access qualifier for given `Var`.

        Also perform various consistency checks.

        Returns True if Final[...] was present.
        """
        if not s.unanalyzed_type or not self.is_final_type(s.unanalyzed_type):
            return False
        assert isinstance(s.unanalyzed_type, UnboundType)
        if len(s.unanalyzed_type.args) > 1:
            self.fail("Final[...] takes at most one type argument", s.unanalyzed_type)
        invalid_bare_final = False
        if not s.unanalyzed_type.args:
            s.type = None
            if (
                isinstance(s.rvalue, TempNode)
                and s.rvalue.no_rhs
                # Filter duplicate errors, we already reported this:
                and not (self.type and self.type.is_named_tuple)
            ):
                invalid_bare_final = True
                self.fail("Type in Final[...] can only be omitted if there is an initializer", s)
        else:
            s.type = s.unanalyzed_type.args[0]

        if (
            s.type is not None
            and self.options.python_version < (3, 13)
            and self.is_classvar(s.type)
        ):
            self.fail("Variable should not be annotated with both ClassVar and Final", s)
            return False

        if len(s.lvalues) != 1 or not isinstance(s.lvalues[0], RefExpr):
            self.fail("Invalid final declaration", s)
            return False
        lval = s.lvalues[0]
        assert isinstance(lval, RefExpr)

        # Reset inferred status if it was set due to simple literal rvalue on previous iteration.
        # TODO: this is a best-effort quick fix, we should avoid the need to manually sync this,
        # see https://github.com/python/mypy/issues/6458.
        if lval.is_new_def:
            lval.is_inferred_def = s.type is None

        if self.loop_depth[-1] > 0:
            self.fail("Cannot use Final inside a loop", s)
        if self.type and self.type.is_protocol:
            if self.is_class_scope():
                self.msg.protocol_members_cant_be_final(s)
        if (
            isinstance(s.rvalue, TempNode)
            and s.rvalue.no_rhs
            and not self.is_stub_file
            and not self.is_class_scope()
        ):
            if not invalid_bare_final:  # Skip extra error messages.
                self.msg.final_without_value(s)
        return True

    def check_final_implicit_def(self, s: AssignmentStmt) -> None:
        """Do basic checks for final declaration on self in __init__.

        Additional re-definition checks are performed by `analyze_lvalue`.
        """
        if not s.is_final_def:
            return
        lval = s.lvalues[0]
        assert isinstance(lval, RefExpr)
        if isinstance(lval, MemberExpr):
            if not self.is_self_member_ref(lval):
                self.fail("Final can be only applied to a name or an attribute on self", s)
                s.is_final_def = False
                return
            else:
                assert self.function_stack
                if self.function_stack[-1].name != "__init__":
                    self.fail("Can only declare a final attribute in class body or __init__", s)
                    s.is_final_def = False
                    return

    def store_final_status(self, s: AssignmentStmt) -> None:
        """If this is a locally valid final declaration, set the corresponding flag on `Var`."""
        if s.is_final_def:
            if len(s.lvalues) == 1 and isinstance(s.lvalues[0], RefExpr):
                node = s.lvalues[0].node
                if isinstance(node, Var):
                    node.is_final = True
                    if s.type:
                        node.final_value = constant_fold_expr(s.rvalue, self.cur_mod_id)
                    if self.is_class_scope() and (
                        isinstance(s.rvalue, TempNode) and s.rvalue.no_rhs
                    ):
                        node.final_unset_in_class = True
        else:
            for lval in self.flatten_lvalues(s.lvalues):
                # Special case: we are working with an `Enum`:
                #
                #   class MyEnum(Enum):
                #       key = 'some value'
                #
                # Here `key` is implicitly final. In runtime, code like
                #
                #     MyEnum.key = 'modified'
                #
                # will fail with `AttributeError: Cannot reassign members.`
                # That's why we need to replicate this.
                if (
                    isinstance(lval, NameExpr)
                    and isinstance(self.type, TypeInfo)
                    and self.type.is_enum
                ):
                    cur_node = self.type.names.get(lval.name, None)
                    if (
                        cur_node
                        and isinstance(cur_node.node, Var)
                        and not (isinstance(s.rvalue, TempNode) and s.rvalue.no_rhs)
                    ):
                        # Double underscored members are writable on an `Enum`.
                        # (Except read-only `__members__` but that is handled in type checker)
                        cur_node.node.is_final = s.is_final_def = not is_dunder(cur_node.node.name)

                # Special case: deferred initialization of a final attribute in __init__.
                # In this case we just pretend this is a valid final definition to suppress
                # errors about assigning to final attribute.
                if isinstance(lval, MemberExpr) and self.is_self_member_ref(lval):
                    assert self.type, "Self member outside a class"
                    cur_node = self.type.names.get(lval.name, None)
                    if cur_node and isinstance(cur_node.node, Var) and cur_node.node.is_final:
                        assert self.function_stack
                        current_function = self.function_stack[-1]
                        if (
                            current_function.name == "__init__"
                            and cur_node.node.final_unset_in_class
                            and not cur_node.node.final_set_in_init
                            and not (isinstance(s.rvalue, TempNode) and s.rvalue.no_rhs)
                        ):
                            cur_node.node.final_set_in_init = True
                            s.is_final_def = True

    def flatten_lvalues(self, lvalues: list[Expression]) -> list[Expression]:
        res: list[Expression] = []
        for lv in lvalues:
            if isinstance(lv, (TupleExpr, ListExpr)):
                res.extend(self.flatten_lvalues(lv.items))
            else:
                res.append(lv)
        return res

    def process_type_annotation(self, s: AssignmentStmt) -> None:
        """Analyze type annotation or infer simple literal type."""
        if s.type:
            lvalue = s.lvalues[-1]
            allow_tuple_literal = isinstance(lvalue, TupleExpr)
            analyzed = self.anal_type(s.type, allow_tuple_literal=allow_tuple_literal)
            # Don't store not ready types (including placeholders).
            if analyzed is None or has_placeholder(analyzed):
                self.defer(s)
                return
            s.type = analyzed
            if (
                self.type
                and self.type.is_protocol
                and isinstance(lvalue, NameExpr)
                and isinstance(s.rvalue, TempNode)
                and s.rvalue.no_rhs
            ):
                if isinstance(lvalue.node, Var):
                    lvalue.node.is_abstract_var = True
        else:
            if (
                self.type
                and self.type.is_protocol
                and self.is_annotated_protocol_member(s)
                and not self.is_func_scope()
            ):
                self.fail("All protocol members must have explicitly declared types", s)
            # Set the type if the rvalue is a simple literal (even if the above error occurred).
            if len(s.lvalues) == 1 and isinstance(s.lvalues[0], RefExpr):
                ref_expr = s.lvalues[0]
                safe_literal_inference = True
                if self.type and isinstance(ref_expr, NameExpr) and len(self.type.mro) > 1:
                    # Check if there is a definition in supertype. If yes, we can't safely
                    # decide here what to infer: int or Literal[42].
                    safe_literal_inference = self.type.mro[1].get(ref_expr.name) is None
                if safe_literal_inference and ref_expr.is_inferred_def:
                    s.type = self.analyze_simple_literal_type(s.rvalue, s.is_final_def)
        if s.type:
            # Store type into nodes.
            for lvalue in s.lvalues:
                self.store_declared_types(lvalue, s.type)

    def is_annotated_protocol_member(self, s: AssignmentStmt) -> bool:
        """Check whether a protocol member is annotated.

        There are some exceptions that can be left unannotated, like ``__slots__``."""
        return any(
            (isinstance(lv, NameExpr) and lv.name != "__slots__" and lv.is_inferred_def)
            for lv in s.lvalues
        )

    def analyze_simple_literal_type(self, rvalue: Expression, is_final: bool) -> Type | None:
        """Return builtins.int if rvalue is an int literal, etc.

        If this is a 'Final' context, we return "Literal[...]" instead.
        """
        if self.function_stack:
            # Skip inside a function; this is to avoid confusing
            # the code that handles dead code due to isinstance()
            # inside type variables with value restrictions (like
            # AnyStr).
            return None

        value = constant_fold_expr(rvalue, self.cur_mod_id)
        if value is None or isinstance(value, complex):
            return None

        if isinstance(value, bool):
            type_name = "builtins.bool"
        elif isinstance(value, int):
            type_name = "builtins.int"
        elif isinstance(value, str):
            type_name = "builtins.str"
        elif isinstance(value, float):
            type_name = "builtins.float"

        typ = self.named_type_or_none(type_name)
        if typ and is_final:
            return typ.copy_modified(last_known_value=LiteralType(value=value, fallback=typ))
        return typ

    def analyze_alias(
        self,
        name: str,
        rvalue: Expression,
        allow_placeholder: bool = False,
        declared_type_vars: TypeVarLikeList | None = None,
        all_declared_type_params_names: list[str] | None = None,
        python_3_12_type_alias: bool = False,
    ) -> tuple[Type | None, list[TypeVarLikeType], set[str], list[str], bool]:
        """Check if 'rvalue' is a valid type allowed for aliasing (e.g. not a type variable).

        If yes, return the corresponding type, a list of
        qualified type variable names for generic aliases, a set of names the alias depends on,
        and a list of type variables if the alias is generic.
        A schematic example for the dependencies:
            A = int
            B = str
            analyze_alias(Dict[A, B])[2] == {'__main__.A', '__main__.B'}
        """
        dynamic = bool(self.function_stack and self.function_stack[-1].is_dynamic())
        global_scope = not self.type and not self.function_stack
        try:
            typ = expr_to_unanalyzed_type(
                rvalue, self.options, self.is_stub_file, lookup_qualified=self.lookup_qualified
            )
        except TypeTranslationError:
            self.fail(
                "Invalid type alias: expression is not a valid type", rvalue, code=codes.VALID_TYPE
            )
            return None, [], set(), [], False

        found_type_vars = self.find_type_var_likes(typ)
        tvar_defs: list[TypeVarLikeType] = []
        namespace = self.qualified_name(name)
        alias_type_vars = found_type_vars if declared_type_vars is None else declared_type_vars
        with self.tvar_scope_frame(self.tvar_scope.class_frame(namespace)):
            tvar_defs = self.tvar_defs_from_tvars(alias_type_vars, typ)

            if python_3_12_type_alias:
                with self.allow_unbound_tvars_set():
                    rvalue.accept(self)

            analyzed, depends_on = analyze_type_alias(
                typ,
                self,
                self.tvar_scope,
                self.plugin,
                self.options,
                self.cur_mod_node,
                self.is_typeshed_stub_file,
                allow_placeholder=allow_placeholder,
                in_dynamic_func=dynamic,
                global_scope=global_scope,
                allowed_alias_tvars=tvar_defs,
                alias_type_params_names=all_declared_type_params_names,
                python_3_12_type_alias=python_3_12_type_alias,
            )

        # There can be only one variadic variable at most, the error is reported elsewhere.
        new_tvar_defs = []
        variadic = False
        for td in tvar_defs:
            if isinstance(td, TypeVarTupleType):
                if variadic:
                    continue
                variadic = True
            new_tvar_defs.append(td)

        qualified_tvars = [node.fullname for _name, node in alias_type_vars]
        empty_tuple_index = typ.empty_tuple_index if isinstance(typ, UnboundType) else False
        return analyzed, new_tvar_defs, depends_on, qualified_tvars, empty_tuple_index

    def is_pep_613(self, s: AssignmentStmt) -> bool:
        if s.unanalyzed_type is not None and isinstance(s.unanalyzed_type, UnboundType):
            lookup = self.lookup_qualified(s.unanalyzed_type.name, s, suppress_errors=True)
            if lookup and lookup.fullname in TYPE_ALIAS_NAMES:
                return True
        return False

    def check_and_set_up_type_alias(self, s: AssignmentStmt) -> bool:
        """Check if assignment creates a type alias and set it up as needed.

        Return True if it is a type alias (even if the target is not ready),
        or False otherwise.

        Note: the resulting types for subscripted (including generic) aliases
        are also stored in rvalue.analyzed.
        """
        if s.invalid_recursive_alias:
            return True
        lvalue = s.lvalues[0]
        if len(s.lvalues) > 1 or not isinstance(lvalue, NameExpr):
            # First rule: Only simple assignments like Alias = ... create aliases.
            return False

        pep_613 = self.is_pep_613(s)
        if not pep_613 and s.unanalyzed_type is not None:
            # Second rule: Explicit type (cls: Type[A] = A) always creates variable, not alias.
            # unless using PEP 613 `cls: TypeAlias = A`
            return False

        # It can be `A = TypeAliasType('A', ...)` call, in this case,
        # we just take the second argument and analyze it:
        type_params: TypeVarLikeList | None
        all_type_params_names: list[str] | None
        if self.check_type_alias_type_call(s.rvalue, name=lvalue.name):
            rvalue = s.rvalue.args[1]
            pep_695 = True
            type_params, all_type_params_names = self.analyze_type_alias_type_params(s.rvalue)
        else:
            rvalue = s.rvalue
            pep_695 = False
            type_params = None
            all_type_params_names = None

        if isinstance(rvalue, CallExpr) and rvalue.analyzed:
            return False

        existing = self.current_symbol_table().get(lvalue.name)
        # Third rule: type aliases can't be re-defined. For example:
        #     A: Type[float] = int
        #     A = float  # OK, but this doesn't define an alias
        #     B = int
        #     B = float  # Error!
        # Don't create an alias in these cases:
        if existing and (
            isinstance(existing.node, Var)  # existing variable
            or (isinstance(existing.node, TypeAlias) and not s.is_alias_def)  # existing alias
            or (isinstance(existing.node, PlaceholderNode) and existing.node.node.line < s.line)
        ):  # previous incomplete definition
            # TODO: find a more robust way to track the order of definitions.
            # Note: if is_alias_def=True, this is just a node from previous iteration.
            if isinstance(existing.node, TypeAlias) and not s.is_alias_def:
                self.fail(
                    'Cannot assign multiple types to name "{}"'
                    ' without an explicit "type[...]" annotation'.format(lvalue.name),
                    lvalue,
                )
            return False

        non_global_scope = self.type or self.is_func_scope()
        if not pep_613 and not pep_695 and isinstance(rvalue, RefExpr) and non_global_scope:
            # Fourth rule (special case): Non-subscripted right hand side creates a variable
            # at class and function scopes. For example:
            #
            #   class Model:
            #       ...
            #   class C:
            #       model = Model # this is automatically a variable with type 'Type[Model]'
            #
            # without this rule, this typical use case will require a lot of explicit
            # annotations (see the second rule).
            return False
        if not pep_613 and not pep_695 and not self.can_be_type_alias(rvalue):
            return False

        if existing and not isinstance(existing.node, (PlaceholderNode, TypeAlias)):
            # Cannot redefine existing node as type alias.
            return False

        res: Type | None = None
        if self.is_none_alias(rvalue):
            res = NoneType()
            alias_tvars: list[TypeVarLikeType] = []
            depends_on: set[str] = set()
            qualified_tvars: list[str] = []
            empty_tuple_index = False
        else:
            tag = self.track_incomplete_refs()
            res, alias_tvars, depends_on, qualified_tvars, empty_tuple_index = self.analyze_alias(
                lvalue.name,
                rvalue,
                allow_placeholder=True,
                declared_type_vars=type_params,
                all_declared_type_params_names=all_type_params_names,
            )
            if not res:
                return False
            if not self.is_func_scope():
                # Only marking incomplete for top-level placeholders makes recursive aliases like
                # `A = Sequence[str | A]` valid here, similar to how we treat base classes in class
                # definitions, allowing `class str(Sequence[str]): ...`
                incomplete_target = isinstance(res, ProperType) and isinstance(
                    res, PlaceholderType
                )
            else:
                incomplete_target = has_placeholder(res)
            if self.found_incomplete_ref(tag) or incomplete_target:
                # Since we have got here, we know this must be a type alias (incomplete refs
                # may appear in nested positions), therefore use becomes_typeinfo=True.
                self.mark_incomplete(lvalue.name, rvalue, becomes_typeinfo=True)
                return True
        self.add_type_alias_deps(depends_on)
        # In addition to the aliases used, we add deps on unbound
        # type variables, since they are erased from target type.
        self.add_type_alias_deps(qualified_tvars)
        # The above are only direct deps on other aliases.
        # For subscripted aliases, type deps from expansion are added in deps.py
        # (because the type is stored).
        check_for_explicit_any(res, self.options, self.is_typeshed_stub_file, self.msg, context=s)
        # When this type alias gets "inlined", the Any is not explicit anymore,
        # so we need to replace it with non-explicit Anys.
        res = make_any_non_explicit(res)
        if self.options.disallow_any_unimported and has_any_from_unimported_type(res):
            # Only show error message once, when the type is fully analyzed.
            if not has_placeholder(res):
                self.msg.unimported_type_becomes_any("Type alias target", res, s)
                res = make_any_non_unimported(res)
        # Note: with the new (lazy) type alias representation we only need to set no_args to True
        # if the expected number of arguments is non-zero, so that aliases like `A = List` work
        # but not aliases like `A = TypeAliasType("A", List)` as these need explicit type params.
        # However, eagerly expanding aliases like Text = str is a nice performance optimization.
        no_args = (
            isinstance(res, ProperType)
            and isinstance(res, Instance)
            and not res.args
            and not empty_tuple_index
            and not pep_695
        )
        if isinstance(res, ProperType) and isinstance(res, Instance):
            if not validate_instance(res, self.fail, empty_tuple_index):
                fix_instance(res, self.fail, self.note, disallow_any=False, options=self.options)
        # Aliases defined within functions can't be accessed outside
        # the function, since the symbol table will no longer
        # exist. Work around by expanding them eagerly when used.
        eager = self.is_func_scope()
        alias_node = TypeAlias(
            res,
            self.qualified_name(lvalue.name),
            s.line,
            s.column,
            alias_tvars=alias_tvars,
            no_args=no_args,
            eager=eager,
            python_3_12_type_alias=pep_695,
        )
        if isinstance(s.rvalue, (IndexExpr, CallExpr, OpExpr)) and (
            not isinstance(rvalue, OpExpr)
            or (self.options.python_version >= (3, 10) or self.is_stub_file)
        ):
            # Note: CallExpr is for "void = type(None)" and OpExpr is for "X | Y" union syntax.
            if not isinstance(s.rvalue.analyzed, TypeAliasExpr):
                # Any existing node will be updated in-place below.
                s.rvalue.analyzed = TypeAliasExpr(alias_node)
            s.rvalue.analyzed.line = s.line
            # we use the column from resulting target, to get better location for errors
            s.rvalue.analyzed.column = res.column
        elif isinstance(s.rvalue, RefExpr):
            s.rvalue.is_alias_rvalue = True

        if existing:
            # An alias gets updated.
            updated = False
            if isinstance(existing.node, TypeAlias):
                if existing.node.target != res:
                    # Copy expansion to the existing alias, this matches how we update base classes
                    # for a TypeInfo _in place_ if there are nested placeholders.
                    existing.node.target = res
                    existing.node.alias_tvars = alias_tvars
                    existing.node.no_args = no_args
                    updated = True
                    # Invalidate recursive status cache in case it was previously set.
                    existing.node._is_recursive = None
            else:
                # Otherwise just replace existing placeholder with type alias.
                existing.node = alias_node
                updated = True
            if updated:
                if self.final_iteration:
                    self.cannot_resolve_name(lvalue.name, "name", s)
                    return True
                else:
                    # We need to defer so that this change can get propagated to base classes.
                    self.defer(s, force_progress=True)
        else:
            self.add_symbol(lvalue.name, alias_node, s)
        if isinstance(rvalue, RefExpr) and isinstance(rvalue.node, TypeAlias):
            alias_node.normalized = rvalue.node.normalized
        current_node = existing.node if existing else alias_node
        assert isinstance(current_node, TypeAlias)
        self.disable_invalid_recursive_aliases(s, current_node, s.rvalue)
        if self.is_class_scope():
            assert self.type is not None
            if self.type.is_protocol:
                self.fail("Type aliases are prohibited in protocol bodies", s)
                if not lvalue.name[0].isupper():
                    self.note("Use variable annotation syntax to define protocol members", s)
        return True

    def check_type_alias_type_call(self, rvalue: Expression, *, name: str) -> TypeGuard[CallExpr]:
        if not isinstance(rvalue, CallExpr):
            return False

        names = ["typing_extensions.TypeAliasType"]
        if self.options.python_version >= (3, 12):
            names.append("typing.TypeAliasType")
        if not refers_to_fullname(rvalue.callee, tuple(names)):
            return False
        if not self.check_typevarlike_name(rvalue, name, rvalue):
            return False
        if rvalue.arg_kinds.count(ARG_POS) != 2:
            return False

        return True

    def analyze_type_alias_type_params(
        self, rvalue: CallExpr
    ) -> tuple[TypeVarLikeList, list[str]]:
        """Analyze type_params of TypeAliasType.

        Returns declared unbound type variable expressions and a list of all declared type
        variable names for error reporting.
        """
        if "type_params" in rvalue.arg_names:
            type_params_arg = rvalue.args[rvalue.arg_names.index("type_params")]
            if not isinstance(type_params_arg, TupleExpr):
                self.fail(
                    "Tuple literal expected as the type_params argument to TypeAliasType",
                    type_params_arg,
                )
                return [], []
            type_params = type_params_arg.items
        else:
            return [], []

        declared_tvars: TypeVarLikeList = []
        all_declared_tvar_names: list[str] = []  # includes bound type variables
        have_type_var_tuple = False
        for tp_expr in type_params:
            if isinstance(tp_expr, StarExpr):
                tp_expr.valid = False
            self.analyze_type_expr(tp_expr)
            try:
                base = self.expr_to_unanalyzed_type(tp_expr)
            except TypeTranslationError:
                continue
            if not isinstance(base, UnboundType):
                continue

            tag = self.track_incomplete_refs()
            tvar = self.analyze_unbound_tvar_impl(base, is_typealias_param=True)
            if tvar:
                if isinstance(tvar[1], TypeVarTupleExpr):
                    if have_type_var_tuple:
                        self.fail(
                            "Can only use one TypeVarTuple in type_params argument to TypeAliasType",
                            base,
                            code=codes.TYPE_VAR,
                        )
                        have_type_var_tuple = True
                        continue
                    have_type_var_tuple = True
            elif not self.found_incomplete_ref(tag):
                sym = self.lookup_qualified(base.name, base)
                if sym and isinstance(sym.node, TypeVarLikeExpr):
                    all_declared_tvar_names.append(sym.node.name)  # Error will be reported later
                else:
                    self.fail(
                        "Free type variable expected in type_params argument to TypeAliasType",
                        base,
                        code=codes.TYPE_VAR,
                    )
                    if sym and sym.fullname in UNPACK_TYPE_NAMES:
                        self.note(
                            "Don't Unpack type variables in type_params", base, code=codes.TYPE_VAR
                        )
                continue
            if tvar in declared_tvars:
                self.fail(
                    f'Duplicate type variable "{tvar[0]}" in type_params argument to TypeAliasType',
                    base,
                    code=codes.TYPE_VAR,
                )
                continue
            if tvar:
                all_declared_tvar_names.append(tvar[0])
                declared_tvars.append(tvar)
        return declared_tvars, all_declared_tvar_names

    def disable_invalid_recursive_aliases(
        self, s: AssignmentStmt | TypeAliasStmt, current_node: TypeAlias, ctx: Context
    ) -> None:
        """Prohibit and fix recursive type aliases that are invalid/unsupported."""
        messages = []
        if (
            isinstance(current_node.target, TypeAliasType)
            and current_node.target.alias is current_node
        ):
            # We want to have consistent error messages, but not calling name_not_defined(),
            # since it will do a bunch of unrelated things we don't want here.
            messages.append(
                f'Cannot resolve name "{current_node.name}" (possible cyclic definition)'
            )
        elif is_invalid_recursive_alias({current_node}, current_node.target):
            target = (
                "tuple" if isinstance(get_proper_type(current_node.target), TupleType) else "union"
            )
            messages.append(f"Invalid recursive alias: a {target} item of itself")
        if detect_diverging_alias(
            current_node, current_node.target, self.lookup_qualified, self.tvar_scope
        ):
            messages.append("Invalid recursive alias: type variable nesting on right hand side")
        if messages:
            current_node.target = AnyType(TypeOfAny.from_error)
            s.invalid_recursive_alias = True
        for msg in messages:
            self.fail(msg, ctx)

    def analyze_lvalue(
        self,
        lval: Lvalue,
        nested: bool = False,
        explicit_type: bool = False,
        is_final: bool = False,
        escape_comprehensions: bool = False,
        has_explicit_value: bool = False,
        is_index_var: bool = False,
    ) -> None:
        """Analyze an lvalue or assignment target.

        Args:
            lval: The target lvalue
            nested: If true, the lvalue is within a tuple or list lvalue expression
            explicit_type: Assignment has type annotation
            escape_comprehensions: If we are inside a comprehension, set the variable
                in the enclosing scope instead. This implements
                https://www.python.org/dev/peps/pep-0572/#scope-of-the-target
            is_index_var: If lval is the index variable in a for loop
        """
        if escape_comprehensions:
            assert isinstance(lval, NameExpr), "assignment expression target must be NameExpr"
        if isinstance(lval, NameExpr):
            self.analyze_name_lvalue(
                lval,
                explicit_type,
                is_final,
                escape_comprehensions,
                has_explicit_value=has_explicit_value,
                is_index_var=is_index_var,
            )
        elif isinstance(lval, MemberExpr):
            self.analyze_member_lvalue(lval, explicit_type, is_final, has_explicit_value)
            if explicit_type and not self.is_self_member_ref(lval):
                self.fail("Type cannot be declared in assignment to non-self attribute", lval)
        elif isinstance(lval, IndexExpr):
            if explicit_type:
                self.fail("Unexpected type declaration", lval)
            lval.accept(self)
        elif isinstance(lval, TupleExpr):
            self.analyze_tuple_or_list_lvalue(lval, explicit_type)
        elif isinstance(lval, StarExpr):
            if nested:
                self.analyze_lvalue(lval.expr, nested, explicit_type)
            else:
                self.fail("Starred assignment target must be in a list or tuple", lval)
        else:
            self.fail("Invalid assignment target", lval)

    def analyze_name_lvalue(
        self,
        lvalue: NameExpr,
        explicit_type: bool,
        is_final: bool,
        escape_comprehensions: bool,
        has_explicit_value: bool,
        is_index_var: bool,
    ) -> None:
        """Analyze an lvalue that targets a name expression.

        Arguments are similar to "analyze_lvalue".
        """
        if lvalue.node:
            # This has been bound already in a previous iteration.
            return

        name = lvalue.name
        if self.is_alias_for_final_name(name):
            if is_final:
                self.fail("Cannot redefine an existing name as final", lvalue)
            else:
                self.msg.cant_assign_to_final(name, self.type is not None, lvalue)

        kind = self.current_symbol_kind()
        names = self.current_symbol_table(escape_comprehensions=escape_comprehensions)
        existing = names.get(name)

        outer = self.is_global_or_nonlocal(name)
        if (
            kind == MDEF
            and isinstance(self.type, TypeInfo)
            and self.type.is_enum
            and not name.startswith("__")
        ):
            # Special case: we need to be sure that `Enum` keys are unique.
            if existing is not None and not isinstance(existing.node, PlaceholderNode):
                self.fail(
                    'Attempted to reuse member name "{}" in Enum definition "{}"'.format(
                        name, self.type.name
                    ),
                    lvalue,
                )

            if explicit_type and has_explicit_value:
                self.fail("Enum members must be left unannotated", lvalue)
                self.note(
                    "See https://typing.readthedocs.io/en/latest/spec/enums.html#defining-members",
                    lvalue,
                )

        if (not existing or isinstance(existing.node, PlaceholderNode)) and not outer:
            # Define new variable.
            var = self.make_name_lvalue_var(
                lvalue, kind, not explicit_type, has_explicit_value, is_index_var
            )
            added = self.add_symbol(name, var, lvalue, escape_comprehensions=escape_comprehensions)
            # Only bind expression if we successfully added name to symbol table.
            if added:
                lvalue.is_new_def = True
                lvalue.is_inferred_def = True
                lvalue.kind = kind
                lvalue.node = var
                if kind == GDEF:
                    lvalue.fullname = var._fullname
                else:
                    lvalue.fullname = lvalue.name
                if self.is_func_scope():
                    if unmangle(name) == "_" and not self.options.allow_redefinition_new:
                        # Special case for assignment to local named '_': always infer 'Any'.
                        # This isn't needed with --allow-redefinition-new, since arbitrary
                        # types can be assigned to '_' anyway.
                        typ = AnyType(TypeOfAny.special_form)
                        self.store_declared_types(lvalue, typ)
            if is_final and self.is_final_redefinition(kind, name):
                self.fail("Cannot redefine an existing name as final", lvalue)
        else:
            self.make_name_lvalue_point_to_existing_def(lvalue, explicit_type, is_final)

    def is_final_redefinition(self, kind: int, name: str) -> bool:
        if kind == GDEF:
            return self.is_mangled_global(name) and not self.is_initial_mangled_global(name)
        elif kind == MDEF and self.type:
            return unmangle(name) + "'" in self.type.names
        return False

    def is_alias_for_final_name(self, name: str) -> bool:
        if self.is_func_scope():
            if not name.endswith("'"):
                # Not a mangled name -- can't be an alias
                return False
            name = unmangle(name)
            assert self.locals[-1] is not None, "No locals at function scope"
            existing = self.locals[-1].get(name)
            return existing is not None and is_final_node(existing.node)
        elif self.type is not None:
            orig_name = unmangle(name) + "'"
            if name == orig_name:
                return False
            existing = self.type.names.get(orig_name)
            return existing is not None and is_final_node(existing.node)
        else:
            orig_name = unmangle(name) + "'"
            if name == orig_name:
                return False
            existing = self.globals.get(orig_name)
            return existing is not None and is_final_node(existing.node)

    def make_name_lvalue_var(
        self,
        lvalue: NameExpr,
        kind: int,
        inferred: bool,
        has_explicit_value: bool,
        is_index_var: bool,
    ) -> Var:
        """Return a Var node for an lvalue that is a name expression."""
        name = lvalue.name
        v = Var(name)
        v.set_line(lvalue)
        v.is_inferred = inferred
        if kind == MDEF:
            assert self.type is not None
            v.info = self.type
            v.is_initialized_in_class = True
            v.allow_incompatible_override = name in ALLOW_INCOMPATIBLE_OVERRIDE
        if kind != LDEF:
            v._fullname = self.qualified_name(name)
        else:
            # fullname should never stay None
            v._fullname = name
        v.is_ready = False  # Type not inferred yet
        v.has_explicit_value = has_explicit_value
        v.is_index_var = is_index_var
        return v

    def make_name_lvalue_point_to_existing_def(
        self, lval: NameExpr, explicit_type: bool, is_final: bool
    ) -> None:
        """Update an lvalue to point to existing definition in the same scope.

        Arguments are similar to "analyze_lvalue".

        Assume that an existing name exists.
        """
        if is_final:
            # Redefining an existing name with final is always an error.
            self.fail("Cannot redefine an existing name as final", lval)
        original_def = self.lookup(lval.name, lval, suppress_errors=True)
        if original_def is None and self.type and not self.is_func_scope():
            # Workaround to allow "x, x = ..." in class body.
            original_def = self.type.get(lval.name)
        if explicit_type:
            # Don't re-bind if there is a type annotation.
            self.name_already_defined(lval.name, lval, original_def)
        else:
            # Bind to an existing name.
            if original_def:
                self.bind_name_expr(lval, original_def)
            else:
                self.name_not_defined(lval.name, lval)
            self.check_lvalue_validity(lval.node, lval)

    def analyze_tuple_or_list_lvalue(self, lval: TupleExpr, explicit_type: bool = False) -> None:
        """Analyze an lvalue or assignment target that is a list or tuple."""
        items = lval.items
        star_exprs = [item for item in items if isinstance(item, StarExpr)]

        if len(star_exprs) > 1:
            self.fail("Two starred expressions in assignment", lval)
        else:
            if len(star_exprs) == 1:
                star_exprs[0].valid = True
            for i in items:
                self.analyze_lvalue(
                    lval=i,
                    nested=True,
                    explicit_type=explicit_type,
                    # Lists and tuples always have explicit values defined:
                    # `a, b, c = value`
                    has_explicit_value=True,
                )

    def analyze_member_lvalue(
        self, lval: MemberExpr, explicit_type: bool, is_final: bool, has_explicit_value: bool
    ) -> None:
        """Analyze lvalue that is a member expression.

        Arguments:
            lval: The target lvalue
            explicit_type: Assignment has type annotation
            is_final: Is the target final
        """
        if lval.node:
            # This has been bound already in a previous iteration.
            return
        lval.accept(self)
        if self.is_self_member_ref(lval):
            assert self.type, "Self member outside a class"
            cur_node = self.type.names.get(lval.name)
            node = self.type.get(lval.name)
            if cur_node and is_final:
                # Overrides will be checked in type checker.
                self.fail("Cannot redefine an existing name as final", lval)
            # On first encounter with this definition, if this attribute was defined before
            # with an inferred type and it's marked with an explicit type now, give an error.
            if (
                not lval.node
                and cur_node
                and isinstance(cur_node.node, Var)
                and cur_node.node.is_inferred
                and explicit_type
            ):
                self.attribute_already_defined(lval.name, lval, cur_node)
            if self.type.is_protocol and has_explicit_value and cur_node is not None:
                # Make this variable non-abstract, it would be safer to do this only if we
                # are inside __init__, but we do this always to preserve historical behaviour.
                if isinstance(cur_node.node, Var):
                    cur_node.node.is_abstract_var = False
            if (
                # If the attribute of self is not defined, create a new Var, ...
                node is None
                # ... or if it is defined as abstract in a *superclass*.
                or (cur_node is None and isinstance(node.node, Var) and node.node.is_abstract_var)
                # ... also an explicit declaration on self also creates a new Var.
                # Note that `explicit_type` might have been erased for bare `Final`,
                # so we also check if `is_final` is passed.
                or (cur_node is None and (explicit_type or is_final))
            ):
                if self.type.is_protocol and node is None:
                    self.fail("Protocol members cannot be defined via assignment to self", lval)
                else:
                    # Implicit attribute definition in __init__.
                    lval.is_new_def = True
                    lval.is_inferred_def = True
                    v = Var(lval.name)
                    v.set_line(lval)
                    v._fullname = self.qualified_name(lval.name)
                    v.info = self.type
                    v.is_ready = False
                    v.explicit_self_type = explicit_type or is_final
                    lval.def_var = v
                    lval.node = v
                    # TODO: should we also set lval.kind = MDEF?
                    self.type.names[lval.name] = SymbolTableNode(MDEF, v, implicit=True)
        self.check_lvalue_validity(lval.node, lval)

    def is_self_member_ref(self, memberexpr: MemberExpr) -> bool:
        """Does memberexpr to refer to an attribute of self?"""
        if not isinstance(memberexpr.expr, NameExpr):
            return False
        node = memberexpr.expr.node
        return isinstance(node, Var) and node.is_self

    def check_lvalue_validity(self, node: Expression | SymbolNode | None, ctx: Context) -> None:
        if isinstance(node, TypeVarExpr):
            self.fail("Invalid assignment target", ctx)
        elif isinstance(node, TypeInfo):
            self.fail(message_registry.CANNOT_ASSIGN_TO_TYPE, ctx)

    def store_declared_types(self, lvalue: Lvalue, typ: Type) -> None:
        if isinstance(lvalue, RefExpr):
            lvalue.is_inferred_def = False
            if isinstance(lvalue.node, Var):
                var = lvalue.node
                var.type = typ
                var.is_ready = True
                typ = get_proper_type(typ)
                if (
                    var.is_final
                    and isinstance(typ, Instance)
                    and typ.last_known_value
                    and (not self.type or not self.type.is_enum)
                ):
                    var.final_value = typ.last_known_value.value
            # If node is not a variable, we'll catch it elsewhere.
        elif isinstance(lvalue, TupleExpr):
            typ = get_proper_type(typ)
            if isinstance(typ, TupleType):
                if len(lvalue.items) != len(typ.items):
                    self.fail("Incompatible number of tuple items", lvalue)
                    return
                for item, itemtype in zip(lvalue.items, typ.items):
                    self.store_declared_types(item, itemtype)
            else:
                self.fail("Tuple type expected for multiple variables", lvalue)
        elif isinstance(lvalue, StarExpr):
            # Historical behavior for the old parser
            self.store_declared_types(lvalue.expr, typ)
        else:
            # This has been flagged elsewhere as an error, so just ignore here.
            pass

    def process_typevar_declaration(self, s: AssignmentStmt) -> bool:
        """Check if s declares a TypeVar; it yes, store it in symbol table.

        Return True if this looks like a type variable declaration (but maybe
        with errors), otherwise return False.
        """
        call = self.get_typevarlike_declaration(s, ("typing.TypeVar", "typing_extensions.TypeVar"))
        if not call:
            return False

        name = self.extract_typevarlike_name(s, call)
        if name is None:
            return False

        # Constraining types
        n_values = call.arg_kinds[1:].count(ARG_POS)
        values = self.analyze_value_types(call.args[1 : 1 + n_values])

        res = self.process_typevar_parameters(
            call.args[1 + n_values :],
            call.arg_names[1 + n_values :],
            call.arg_kinds[1 + n_values :],
            n_values,
            s,
        )
        if res is None:
            return False
        variance, upper_bound, default = res

        existing = self.current_symbol_table().get(name)
        if existing and not (
            isinstance(existing.node, PlaceholderNode)
            or
            # Also give error for another type variable with the same name.
            (isinstance(existing.node, TypeVarExpr) and existing.node is call.analyzed)
        ):
            self.fail(f'Cannot redefine "{name}" as a type variable', s)
            return False

        if self.options.disallow_any_unimported:
            for idx, constraint in enumerate(values, start=1):
                if has_any_from_unimported_type(constraint):
                    prefix = f"Constraint {idx}"
                    self.msg.unimported_type_becomes_any(prefix, constraint, s)

            if has_any_from_unimported_type(upper_bound):
                prefix = "Upper bound of type variable"
                self.msg.unimported_type_becomes_any(prefix, upper_bound, s)

        for t in values + [upper_bound, default]:
            check_for_explicit_any(
                t, self.options, self.is_typeshed_stub_file, self.msg, context=s
            )

        # mypyc suppresses making copies of a function to check each
        # possible type, so set the upper bound to Any to prevent that
        # from causing errors.
        if values and self.options.mypyc:
            upper_bound = AnyType(TypeOfAny.implementation_artifact)

        # Yes, it's a valid type variable definition! Add it to the symbol table.
        if not call.analyzed:
            type_var = TypeVarExpr(
                name, self.qualified_name(name), values, upper_bound, default, variance
            )
            type_var.line = call.line
            call.analyzed = type_var
            updated = True
        else:
            assert isinstance(call.analyzed, TypeVarExpr)
            updated = (
                values != call.analyzed.values
                or upper_bound != call.analyzed.upper_bound
                or default != call.analyzed.default
            )
            call.analyzed.upper_bound = upper_bound
            call.analyzed.values = values
            call.analyzed.default = default
        if any(has_placeholder(v) for v in values):
            self.process_placeholder(None, "TypeVar values", s, force_progress=updated)
        elif has_placeholder(upper_bound):
            self.process_placeholder(None, "TypeVar upper bound", s, force_progress=updated)
        elif has_placeholder(default):
            self.process_placeholder(None, "TypeVar default", s, force_progress=updated)

        self.add_symbol(name, call.analyzed, s)
        return True

    def check_typevar_default(self, default: Type, context: Context) -> Type:
        typ = get_proper_type(default)
        if isinstance(typ, AnyType) and typ.is_from_error:
            self.fail(
                message_registry.TYPEVAR_ARG_MUST_BE_TYPE.format("TypeVar", "default"), context
            )
        return default

    def check_paramspec_default(self, default: Type, context: Context) -> Type:
        typ = get_proper_type(default)
        if isinstance(typ, Parameters):
            for i, arg_type in enumerate(typ.arg_types):
                arg_ptype = get_proper_type(arg_type)
                if isinstance(arg_ptype, AnyType) and arg_ptype.is_from_error:
                    self.fail(f"Argument {i} of ParamSpec default must be a type", context)
        elif (
            isinstance(typ, AnyType)
            and typ.is_from_error
            or not isinstance(typ, (AnyType, UnboundType))
        ):
            self.fail(
                "The default argument to ParamSpec must be a list expression, ellipsis, or a ParamSpec",
                context,
            )
            default = AnyType(TypeOfAny.from_error)
        return default

    def check_typevartuple_default(self, default: Type, context: Context) -> Type:
        typ = get_proper_type(default)
        if not isinstance(typ, UnpackType):
            self.fail("The default argument to TypeVarTuple must be an Unpacked tuple", context)
            default = AnyType(TypeOfAny.from_error)
        return default

    def check_typevarlike_name(self, call: CallExpr, name: str, context: Context) -> bool:
        """Checks that the name of a TypeVar or ParamSpec matches its variable."""
        name = unmangle(name)
        assert isinstance(call.callee, RefExpr)
        typevarlike_type = (
            call.callee.name if isinstance(call.callee, NameExpr) else call.callee.fullname
        )
        if len(call.args) < 1:
            self.fail(f"Too few arguments for {typevarlike_type}()", context)
            return False
        if not isinstance(call.args[0], StrExpr) or call.arg_kinds[0] != ARG_POS:
            self.fail(f"{typevarlike_type}() expects a string literal as first argument", context)
            return False
        elif call.args[0].value != name:
            msg = 'String argument 1 "{}" to {}(...) does not match variable name "{}"'
            self.fail(msg.format(call.args[0].value, typevarlike_type, name), context)
            return False
        return True

    def get_typevarlike_declaration(
        self, s: AssignmentStmt, typevarlike_types: tuple[str, ...]
    ) -> CallExpr | None:
        """Returns the call expression if `s` is a declaration of `typevarlike_type`
        (TypeVar or ParamSpec), or None otherwise.
        """
        if len(s.lvalues) != 1 or not isinstance(s.lvalues[0], NameExpr):
            return None
        if not isinstance(s.rvalue, CallExpr):
            return None
        call = s.rvalue
        callee = call.callee
        if not isinstance(callee, RefExpr):
            return None
        if callee.fullname not in typevarlike_types:
            return None
        return call

    def process_typevar_parameters(
        self,
        args: list[Expression],
        names: list[str | None],
        kinds: list[ArgKind],
        num_values: int,
        context: Context,
    ) -> tuple[int, Type, Type] | None:
        has_values = num_values > 0
        covariant = False
        contravariant = False
        upper_bound: Type = self.object_type()
        default: Type = AnyType(TypeOfAny.from_omitted_generics)
        for param_value, param_name, param_kind in zip(args, names, kinds):
            if not param_kind.is_named():
                self.fail(message_registry.TYPEVAR_UNEXPECTED_ARGUMENT, context)
                return None
            if param_name == "covariant":
                if isinstance(param_value, NameExpr) and param_value.name in ("True", "False"):
                    covariant = param_value.name == "True"
                else:
                    self.fail(message_registry.TYPEVAR_VARIANCE_DEF.format("covariant"), context)
                    return None
            elif param_name == "contravariant":
                if isinstance(param_value, NameExpr) and param_value.name in ("True", "False"):
                    contravariant = param_value.name == "True"
                else:
                    self.fail(
                        message_registry.TYPEVAR_VARIANCE_DEF.format("contravariant"), context
                    )
                    return None
            elif param_name == "bound":
                if has_values:
                    self.fail("TypeVar cannot have both values and an upper bound", context)
                    return None
                tv_arg = self.get_typevarlike_argument("TypeVar", param_name, param_value, context)
                if tv_arg is None:
                    return None
                upper_bound = tv_arg
            elif param_name == "default":
                tv_arg = self.get_typevarlike_argument(
                    "TypeVar", param_name, param_value, context, allow_unbound_tvars=True
                )
                default = tv_arg or AnyType(TypeOfAny.from_error)
            elif param_name == "values":
                # Probably using obsolete syntax with values=(...). Explain the current syntax.
                self.fail('TypeVar "values" argument not supported', context)
                self.fail(
                    "Use TypeVar('T', t, ...) instead of TypeVar('T', values=(t, ...))", context
                )
                return None
            else:
                self.fail(
                    f'{message_registry.TYPEVAR_UNEXPECTED_ARGUMENT}: "{param_name}"', context
                )
                return None

        if covariant and contravariant:
            self.fail("TypeVar cannot be both covariant and contravariant", context)
            return None
        elif num_values == 1:
            self.fail(message_registry.TYPE_VAR_TOO_FEW_CONSTRAINED_TYPES, context)
            return None
        elif covariant:
            variance = COVARIANT
        elif contravariant:
            variance = CONTRAVARIANT
        else:
            variance = INVARIANT
        return variance, upper_bound, default

    def get_typevarlike_argument(
        self,
        typevarlike_name: str,
        param_name: str,
        param_value: Expression,
        context: Context,
        *,
        allow_unbound_tvars: bool = False,
        allow_param_spec_literals: bool = False,
        allow_unpack: bool = False,
        report_invalid_typevar_arg: bool = True,
    ) -> ProperType | None:
        try:
            # We want to use our custom error message below, so we suppress
            # the default error message for invalid types here.
            analyzed = self.expr_to_analyzed_type(
                param_value,
                allow_placeholder=True,
                report_invalid_types=False,
                allow_unbound_tvars=allow_unbound_tvars,
                allow_param_spec_literals=allow_param_spec_literals,
                allow_unpack=allow_unpack,
            )
            if analyzed is None:
                # Type variables are special: we need to place them in the symbol table
                # soon, even if upper bound is not ready yet. Otherwise avoiding
                # a "deadlock" in this common pattern would be tricky:
                #     T = TypeVar('T', bound=Custom[Any])
                #     class Custom(Generic[T]):
                #         ...
                analyzed = PlaceholderType(None, [], context.line)
            typ = get_proper_type(analyzed)
            if report_invalid_typevar_arg and isinstance(typ, AnyType) and typ.is_from_error:
                self.fail(
                    message_registry.TYPEVAR_ARG_MUST_BE_TYPE.format(typevarlike_name, param_name),
                    param_value,
                )
                # Note: we do not return 'None' here -- we want to continue
                # using the AnyType.
            return typ
        except TypeTranslationError:
            if report_invalid_typevar_arg:
                self.fail(
                    message_registry.TYPEVAR_ARG_MUST_BE_TYPE.format(typevarlike_name, param_name),
                    param_value,
                )
            return None

    def extract_typevarlike_name(self, s: AssignmentStmt, call: CallExpr) -> str | None:
        if not call:
            return None

        lvalue = s.lvalues[0]
        assert isinstance(lvalue, NameExpr)
        if s.type:
            self.fail("Cannot declare the type of a TypeVar or similar construct", s)
            return None

        if not self.check_typevarlike_name(call, lvalue.name, s):
            return None
        return lvalue.name

    def process_paramspec_declaration(self, s: AssignmentStmt) -> bool:
        """Checks if s declares a ParamSpec; if yes, store it in symbol table.

        Return True if this looks like a ParamSpec (maybe with errors), otherwise return False.

        In the future, ParamSpec may accept bounds and variance arguments, in which
        case more aggressive sharing of code with process_typevar_declaration should be pursued.
        """
        call = self.get_typevarlike_declaration(
            s, ("typing_extensions.ParamSpec", "typing.ParamSpec")
        )
        if not call:
            return False

        name = self.extract_typevarlike_name(s, call)
        if name is None:
            return False

        n_values = call.arg_kinds[1:].count(ARG_POS)
        if n_values != 0:
            self.fail('Too many positional arguments for "ParamSpec"', s)

        default: Type = AnyType(TypeOfAny.from_omitted_generics)
        for param_value, param_name in zip(
            call.args[1 + n_values :], call.arg_names[1 + n_values :]
        ):
            if param_name == "default":
                tv_arg = self.get_typevarlike_argument(
                    "ParamSpec",
                    param_name,
                    param_value,
                    s,
                    allow_unbound_tvars=True,
                    allow_param_spec_literals=True,
                    report_invalid_typevar_arg=False,
                )
                default = tv_arg or AnyType(TypeOfAny.from_error)
                default = self.check_paramspec_default(default, param_value)
            else:
                # ParamSpec is different from a regular TypeVar:
                # arguments are not semantically valid. But, allowed in runtime.
                # So, we need to warn users about possible invalid usage.
                self.fail(
                    "The variance and bound arguments to ParamSpec do not have defined semantics yet",
                    s,
                )

        # PEP 612 reserves the right to define bound, covariant and contravariant arguments to
        # ParamSpec in a later PEP. If and when that happens, we should do something
        # on the lines of process_typevar_parameters

        if not call.analyzed:
            paramspec_var = ParamSpecExpr(
                name, self.qualified_name(name), self.object_type(), default, INVARIANT
            )
            paramspec_var.line = call.line
            call.analyzed = paramspec_var
            updated = True
        else:
            assert isinstance(call.analyzed, ParamSpecExpr)
            updated = default != call.analyzed.default
            call.analyzed.default = default
        if has_placeholder(default):
            self.process_placeholder(None, "ParamSpec default", s, force_progress=updated)

        self.add_symbol(name, call.analyzed, s)
        return True

    def process_typevartuple_declaration(self, s: AssignmentStmt) -> bool:
        """Checks if s declares a TypeVarTuple; if yes, store it in symbol table.

        Return True if this looks like a TypeVarTuple (maybe with errors), otherwise return False.
        """
        call = self.get_typevarlike_declaration(
            s, ("typing_extensions.TypeVarTuple", "typing.TypeVarTuple")
        )
        if not call:
            return False

        n_values = call.arg_kinds[1:].count(ARG_POS)
        if n_values != 0:
            self.fail('Too many positional arguments for "TypeVarTuple"', s)

        default: Type = AnyType(TypeOfAny.from_omitted_generics)
        for param_value, param_name in zip(
            call.args[1 + n_values :], call.arg_names[1 + n_values :]
        ):
            if param_name == "default":
                tv_arg = self.get_typevarlike_argument(
                    "TypeVarTuple",
                    param_name,
                    param_value,
                    s,
                    allow_unbound_tvars=True,
                    report_invalid_typevar_arg=False,
                    allow_unpack=True,
                )
                default = tv_arg or AnyType(TypeOfAny.from_error)
                default = self.check_typevartuple_default(default, param_value)
            else:
                self.fail(f'Unexpected keyword argument "{param_name}" for "TypeVarTuple"', s)

        name = self.extract_typevarlike_name(s, call)
        if name is None:
            return False

        # PEP 646 does not specify the behavior of variance, constraints, or bounds.
        if not call.analyzed:
            tuple_fallback = self.named_type("builtins.tuple", [self.object_type()])
            typevartuple_var = TypeVarTupleExpr(
                name,
                self.qualified_name(name),
                # Upper bound for *Ts is *tuple[object, ...], it can never be object.
                tuple_fallback.copy_modified(),
                tuple_fallback,
                default,
                INVARIANT,
            )
            typevartuple_var.line = call.line
            call.analyzed = typevartuple_var
            updated = True
        else:
            assert isinstance(call.analyzed, TypeVarTupleExpr)
            updated = default != call.analyzed.default
            call.analyzed.default = default
        if has_placeholder(default):
            self.process_placeholder(None, "TypeVarTuple default", s, force_progress=updated)

        self.add_symbol(name, call.analyzed, s)
        return True

    def basic_new_typeinfo(self, name: str, basetype_or_fallback: Instance, line: int) -> TypeInfo:
        if self.is_func_scope() and not self.type and "@" not in name:
            name += "@" + str(line)
        class_def = ClassDef(name, Block([]))
        if self.is_func_scope() and not self.type:
            # Full names of generated classes should always be prefixed with the module names
            # even if they are nested in a function, since these classes will be (de-)serialized.
            # (Note that the caller should append @line to the name to avoid collisions.)
            # TODO: clean this up, see #6422.
            class_def.fullname = self.cur_mod_id + "." + self.qualified_name(name)
        else:
            class_def.fullname = self.qualified_name(name)

        info = TypeInfo(SymbolTable(), class_def, self.cur_mod_id)
        class_def.info = info
        mro = basetype_or_fallback.type.mro
        if not mro:
            # Probably an error, we should not crash so generate something meaningful.
            mro = [basetype_or_fallback.type, self.object_type().type]
        info.mro = [info] + mro
        info.bases = [basetype_or_fallback]
        return info

    def analyze_value_types(self, items: list[Expression]) -> list[Type]:
        """Analyze types from values expressions in type variable definition."""
        result: list[Type] = []
        for node in items:
            try:
                analyzed = self.anal_type(
                    self.expr_to_unanalyzed_type(node), allow_placeholder=True
                )
                if analyzed is None:
                    # Type variables are special: we need to place them in the symbol table
                    # soon, even if some value is not ready yet, see process_typevar_parameters()
                    # for an example.
                    analyzed = PlaceholderType(None, [], node.line)
                if has_type_vars(analyzed):
                    self.fail(message_registry.TYPE_VAR_GENERIC_CONSTRAINT_TYPE, node)
                    result.append(AnyType(TypeOfAny.from_error))
                else:
                    result.append(analyzed)
            except TypeTranslationError:
                self.fail("Type expected", node)
                result.append(AnyType(TypeOfAny.from_error))
        return result

    def check_classvar(self, s: AssignmentStmt) -> None:
        """Check if assignment defines a class variable."""
        lvalue = s.lvalues[0]
        if len(s.lvalues) != 1 or not isinstance(lvalue, RefExpr):
            return
        if not s.type or not self.is_classvar(s.type):
            return
        if self.is_class_scope() and isinstance(lvalue, NameExpr):
            node = lvalue.node
            if isinstance(node, Var):
                node.is_classvar = True
            analyzed = self.anal_type(s.type)
            assert self.type is not None
            if (
                analyzed is not None
                and self.type.self_type in get_type_vars(analyzed)
                and self.type.defn.type_vars
            ):
                self.fail(message_registry.CLASS_VAR_WITH_GENERIC_SELF, s)
        elif not isinstance(lvalue, MemberExpr) or self.is_self_member_ref(lvalue):
            # In case of member access, report error only when assigning to self
            # Other kinds of member assignments should be already reported
            self.fail_invalid_classvar(lvalue)

    def is_classvar(self, typ: Type) -> bool:
        if not isinstance(typ, UnboundType):
            return False
        sym = self.lookup_qualified(typ.name, typ)
        if not sym or not sym.node:
            return False
        return sym.node.fullname == "typing.ClassVar"

    def is_final_type(self, typ: Type | None) -> bool:
        if not isinstance(typ, UnboundType):
            return False
        sym = self.lookup_qualified(typ.name, typ)
        if not sym or not sym.node:
            return False
        return sym.node.fullname in FINAL_TYPE_NAMES

    def fail_invalid_classvar(self, context: Context) -> None:
        self.fail(message_registry.CLASS_VAR_OUTSIDE_OF_CLASS, context)

    def process_module_assignment(
        self, lvals: list[Lvalue], rval: Expression, ctx: AssignmentStmt
    ) -> None:
        """Propagate module references across assignments.

        Recursively handles the simple form of iterable unpacking; doesn't
        handle advanced unpacking with *rest, dictionary unpacking, etc.

        In an expression like x = y = z, z is the rval and lvals will be [x,
        y].

        """
        if isinstance(rval, (TupleExpr, ListExpr)) and all(
            isinstance(v, TupleExpr) for v in lvals
        ):
            # rval and all lvals are either list or tuple, so we are dealing
            # with unpacking assignment like `x, y = a, b`. Mypy didn't
            # understand our all(isinstance(...)), so cast them as TupleExpr
            # so mypy knows it is safe to access their .items attribute.
            seq_lvals = cast(list[TupleExpr], lvals)
            # given an assignment like:
            #     (x, y) = (m, n) = (a, b)
            # we now have:
            #     seq_lvals = [(x, y), (m, n)]
            #     seq_rval = (a, b)
            # We now zip this into:
            #     elementwise_assignments = [(a, x, m), (b, y, n)]
            # where each elementwise assignment includes one element of rval and the
            # corresponding element of each lval. Basically we unpack
            #     (x, y) = (m, n) = (a, b)
            # into elementwise assignments
            #     x = m = a
            #     y = n = b
            # and then we recursively call this method for each of those assignments.
            # If the rval and all lvals are not all of the same length, zip will just ignore
            # extra elements, so no error will be raised here; mypy will later complain
            # about the length mismatch in type-checking.
            elementwise_assignments = zip(rval.items, *[v.items for v in seq_lvals])
            for rv, *lvs in elementwise_assignments:
                self.process_module_assignment(lvs, rv, ctx)
        elif isinstance(rval, RefExpr):
            rnode = self.lookup_type_node(rval)
            if rnode and isinstance(rnode.node, MypyFile):
                for lval in lvals:
                    if not isinstance(lval, RefExpr):
                        continue
                    # respect explicitly annotated type
                    if isinstance(lval.node, Var) and lval.node.type is not None:
                        continue

                    # We can handle these assignments to locals and to self
                    if isinstance(lval, NameExpr):
                        lnode = self.current_symbol_table().get(lval.name)
                    elif isinstance(lval, MemberExpr) and self.is_self_member_ref(lval):
                        assert self.type is not None
                        lnode = self.type.names.get(lval.name)
                    else:
                        continue

                    if lnode:
                        if isinstance(lnode.node, MypyFile) and lnode.node is not rnode.node:
                            assert isinstance(lval, (NameExpr, MemberExpr))
                            self.fail(
                                'Cannot assign multiple modules to name "{}" '
                                'without explicit "types.ModuleType" annotation'.format(lval.name),
                                ctx,
                            )
                        # never create module alias except on initial var definition
                        elif lval.is_inferred_def:
                            assert rnode.node is not None
                            lnode.node = rnode.node

    def process__all__(self, s: AssignmentStmt) -> None:
        """Export names if argument is a __all__ assignment."""
        if (
            len(s.lvalues) == 1
            and isinstance(s.lvalues[0], NameExpr)
            and s.lvalues[0].name == "__all__"
            and s.lvalues[0].kind == GDEF
            and isinstance(s.rvalue, (ListExpr, TupleExpr))
        ):
            self.add_exports(s.rvalue.items)

    def process__deletable__(self, s: AssignmentStmt) -> None:
        if not self.options.mypyc:
            return
        if (
            len(s.lvalues) == 1
            and isinstance(s.lvalues[0], NameExpr)
            and s.lvalues[0].name == "__deletable__"
            and s.lvalues[0].kind == MDEF
        ):
            rvalue = s.rvalue
            if not isinstance(rvalue, (ListExpr, TupleExpr)):
                self.fail('"__deletable__" must be initialized with a list or tuple expression', s)
                return
            items = rvalue.items
            attrs = []
            for item in items:
                if not isinstance(item, StrExpr):
                    self.fail('Invalid "__deletable__" item; string literal expected', item)
                else:
                    attrs.append(item.value)
            assert self.type
            self.type.deletable_attributes = attrs

    def process__slots__(self, s: AssignmentStmt) -> None:
        """
        Processing ``__slots__`` if defined in type.

        See: https://docs.python.org/3/reference/datamodel.html#slots
        """
        # Later we can support `__slots__` defined as `__slots__ = other = ('a', 'b')`
        if (
            isinstance(self.type, TypeInfo)
            and len(s.lvalues) == 1
            and isinstance(s.lvalues[0], NameExpr)
            and s.lvalues[0].name == "__slots__"
            and s.lvalues[0].kind == MDEF
        ):
            # We understand `__slots__` defined as string, tuple, list, set, and dict:
            if not isinstance(s.rvalue, (StrExpr, ListExpr, TupleExpr, SetExpr, DictExpr)):
                # For example, `__slots__` can be defined as a variable,
                # we don't support it for now.
                return

            if any(p.slots is None for p in self.type.mro[1:-1]):
                # At least one type in mro (excluding `self` and `object`)
                # does not have concrete `__slots__` defined. Ignoring.
                return

            concrete_slots = True
            rvalue: list[Expression] = []
            if isinstance(s.rvalue, StrExpr):
                rvalue.append(s.rvalue)
            elif isinstance(s.rvalue, (ListExpr, TupleExpr, SetExpr)):
                rvalue.extend(s.rvalue.items)
            else:
                # We have a special treatment of `dict` with possible `{**kwargs}` usage.
                # In this case we consider all `__slots__` to be non-concrete.
                for key, _ in s.rvalue.items:
                    if concrete_slots and key is not None:
                        rvalue.append(key)
                    else:
                        concrete_slots = False

            slots = []
            for item in rvalue:
                # Special case for `'__dict__'` value:
                # when specified it will still allow any attribute assignment.
                if isinstance(item, StrExpr) and item.value != "__dict__":
                    slots.append(item.value)
                else:
                    concrete_slots = False
            if not concrete_slots:
                # Some slot items are dynamic, we don't want any false positives,
                # so, we just pretend that this type does not have any slots at all.
                return

            # We need to copy all slots from super types:
            for super_type in self.type.mro[1:-1]:
                assert super_type.slots is not None
                slots.extend(super_type.slots)
            self.type.slots = set(slots)

    #
    # Misc statements
    #

    def visit_block(self, b: Block) -> None:
        if b.is_unreachable:
            return
        self.block_depth[-1] += 1
        for s in b.body:
            self.accept(s)
        self.block_depth[-1] -= 1

    def visit_block_maybe(self, b: Block | None) -> None:
        if b:
            self.visit_block(b)

    def visit_expression_stmt(self, s: ExpressionStmt) -> None:
        self.statement = s
        s.expr.accept(self)

    def visit_return_stmt(self, s: ReturnStmt) -> None:
        self.statement = s
        if not self.is_func_scope():
            self.fail('"return" outside function', s)
        if self.return_stmt_inside_except_star_block:
            self.fail('"return" not allowed in except* block', s, serious=True)
        if s.expr:
            s.expr.accept(self)

    def visit_raise_stmt(self, s: RaiseStmt) -> None:
        self.statement = s
        if s.expr:
            s.expr.accept(self)
        if s.from_expr:
            s.from_expr.accept(self)

    def visit_assert_stmt(self, s: AssertStmt) -> None:
        self.statement = s
        if s.expr:
            s.expr.accept(self)
        if s.msg:
            s.msg.accept(self)

    def visit_operator_assignment_stmt(self, s: OperatorAssignmentStmt) -> None:
        self.statement = s
        s.lvalue.accept(self)
        s.rvalue.accept(self)
        if (
            isinstance(s.lvalue, NameExpr)
            and s.lvalue.name == "__all__"
            and s.lvalue.kind == GDEF
            and isinstance(s.rvalue, (ListExpr, TupleExpr))
        ):
            self.add_exports(s.rvalue.items)

    def visit_while_stmt(self, s: WhileStmt) -> None:
        self.statement = s
        s.expr.accept(self)
        self.loop_depth[-1] += 1
        with self.inside_except_star_block_set(value=False, entering_loop=True):
            s.body.accept(self)
        self.loop_depth[-1] -= 1
        self.visit_block_maybe(s.else_body)

    def visit_for_stmt(self, s: ForStmt) -> None:
        if s.is_async:
            if not self.is_func_scope() or not self.function_stack[-1].is_coroutine:
                self.fail(message_registry.ASYNC_FOR_OUTSIDE_COROUTINE, s, code=codes.SYNTAX)

        self.statement = s
        s.expr.accept(self)

        # Bind index variables and check if they define new names.
        self.analyze_lvalue(s.index, explicit_type=s.index_type is not None, is_index_var=True)
        if s.index_type:
            if self.is_classvar(s.index_type):
                self.fail_invalid_classvar(s.index)
            allow_tuple_literal = isinstance(s.index, TupleExpr)
            analyzed = self.anal_type(s.index_type, allow_tuple_literal=allow_tuple_literal)
            if analyzed is not None:
                self.store_declared_types(s.index, analyzed)
                s.index_type = analyzed

        self.loop_depth[-1] += 1
        with self.inside_except_star_block_set(value=False, entering_loop=True):
            self.visit_block(s.body)
        self.loop_depth[-1] -= 1
        self.visit_block_maybe(s.else_body)

    def visit_break_stmt(self, s: BreakStmt) -> None:
        self.statement = s
        if self.loop_depth[-1] == 0:
            self.fail('"break" outside loop', s, serious=True, blocker=True)
        if self.inside_except_star_block:
            self.fail('"break" not allowed in except* block', s, serious=True)

    def visit_continue_stmt(self, s: ContinueStmt) -> None:
        self.statement = s
        if self.loop_depth[-1] == 0:
            self.fail('"continue" outside loop', s, serious=True, blocker=True)
        if self.inside_except_star_block:
            self.fail('"continue" not allowed in except* block', s, serious=True)

    def visit_if_stmt(self, s: IfStmt) -> None:
        self.statement = s
        infer_reachability_of_if_statement(s, self.options)
        for i in range(len(s.expr)):
            s.expr[i].accept(self)
            self.visit_block(s.body[i])
        self.visit_block_maybe(s.else_body)

    def visit_try_stmt(self, s: TryStmt) -> None:
        self.statement = s
        self.analyze_try_stmt(s, self)

    def analyze_try_stmt(self, s: TryStmt, visitor: NodeVisitor[None]) -> None:
        s.body.accept(visitor)
        for type, var, handler in zip(s.types, s.vars, s.handlers):
            if type:
                type.accept(visitor)
            if var:
                self.analyze_lvalue(var)
            with self.inside_except_star_block_set(self.inside_except_star_block or s.is_star):
                handler.accept(visitor)
        if s.else_body:
            s.else_body.accept(visitor)
        if s.finally_body:
            s.finally_body.accept(visitor)

    def visit_with_stmt(self, s: WithStmt) -> None:
        self.statement = s
        types: list[Type] = []

        if s.is_async:
            if not self.is_func_scope() or not self.function_stack[-1].is_coroutine:
                self.fail(message_registry.ASYNC_WITH_OUTSIDE_COROUTINE, s, code=codes.SYNTAX)

        if s.unanalyzed_type:
            assert isinstance(s.unanalyzed_type, ProperType)
            actual_targets = [t for t in s.target if t is not None]
            if len(actual_targets) == 0:
                # We have a type for no targets
                self.fail('Invalid type comment: "with" statement has no targets', s)
            elif len(actual_targets) == 1:
                # We have one target and one type
                types = [s.unanalyzed_type]
            elif isinstance(s.unanalyzed_type, TupleType):
                # We have multiple targets and multiple types
                if len(actual_targets) == len(s.unanalyzed_type.items):
                    types = s.unanalyzed_type.items.copy()
                else:
                    # But it's the wrong number of items
                    self.fail('Incompatible number of types for "with" targets', s)
            else:
                # We have multiple targets and one type
                self.fail('Multiple types expected for multiple "with" targets', s)

        new_types: list[Type] = []
        for e, n in zip(s.expr, s.target):
            e.accept(self)
            if n:
                self.analyze_lvalue(n, explicit_type=s.unanalyzed_type is not None)

                # Since we have a target, pop the next type from types
                if types:
                    t = types.pop(0)
                    if self.is_classvar(t):
                        self.fail_invalid_classvar(n)
                    allow_tuple_literal = isinstance(n, TupleExpr)
                    analyzed = self.anal_type(t, allow_tuple_literal=allow_tuple_literal)
                    if analyzed is not None:
                        # TODO: Deal with this better
                        new_types.append(analyzed)
                        self.store_declared_types(n, analyzed)

        s.analyzed_types = new_types

        self.visit_block(s.body)

    def visit_del_stmt(self, s: DelStmt) -> None:
        self.statement = s
        s.expr.accept(self)
        if not self.is_valid_del_target(s.expr):
            self.fail("Invalid delete target", s)

    def is_valid_del_target(self, s: Expression) -> bool:
        if isinstance(s, (IndexExpr, NameExpr, MemberExpr)):
            return True
        elif isinstance(s, (TupleExpr, ListExpr)):
            return all(self.is_valid_del_target(item) for item in s.items)
        else:
            return False

    def visit_global_decl(self, g: GlobalDecl) -> None:
        self.statement = g
        for name in g.names:
            if name in self.nonlocal_decls[-1]:
                self.fail(f'Name "{name}" is nonlocal and global', g)
            self.global_decls[-1].add(name)

    def visit_nonlocal_decl(self, d: NonlocalDecl) -> None:
        self.statement = d
        if self.is_module_scope():
            self.fail("nonlocal declaration not allowed at module level", d)
        else:
            for name in d.names:
                for table, scope_type in zip(
                    reversed(self.locals[:-1]), reversed(self.scope_stack[:-1])
                ):
                    if table is not None and name in table:
                        if scope_type == SCOPE_ANNOTATION:
                            self.fail(
                                f'nonlocal binding not allowed for type parameter "{name}"', d
                            )
                        break
                else:
                    self.fail(f'No binding for nonlocal "{name}" found', d)

                if self.locals[-1] is not None and name in self.locals[-1]:
                    self.fail(
                        'Name "{}" is already defined in local '
                        "scope before nonlocal declaration".format(name),
                        d,
                    )

                if name in self.global_decls[-1]:
                    self.fail(f'Name "{name}" is nonlocal and global', d)
                self.nonlocal_decls[-1].add(name)

    def visit_match_stmt(self, s: MatchStmt) -> None:
        self.statement = s
        infer_reachability_of_match_statement(s, self.options)
        s.subject.accept(self)
        for i in range(len(s.patterns)):
            s.patterns[i].accept(self)
            guard = s.guards[i]
            if guard is not None:
                guard.accept(self)
            self.visit_block(s.bodies[i])

    def visit_type_alias_stmt(self, s: TypeAliasStmt) -> None:
        if s.invalid_recursive_alias:
            return
        self.statement = s
        type_params = self.push_type_args(s.type_args, s)
        if type_params is None:
            self.defer(s)
            return
        all_type_params_names = [p.name for p in s.type_args]

        try:
            existing = self.current_symbol_table().get(s.name.name)
            if existing and not (
                isinstance(existing.node, TypeAlias)
                or (isinstance(existing.node, PlaceholderNode) and existing.node.line == s.line)
            ):
                self.already_defined(s.name.name, s, existing, "Name")
                return

            tag = self.track_incomplete_refs()
            res, alias_tvars, depends_on, qualified_tvars, empty_tuple_index = self.analyze_alias(
                s.name.name,
                s.value.expr(),
                allow_placeholder=True,
                declared_type_vars=type_params,
                all_declared_type_params_names=all_type_params_names,
                python_3_12_type_alias=True,
            )
            if not res:
                res = AnyType(TypeOfAny.from_error)

            if not self.is_func_scope():
                # Only marking incomplete for top-level placeholders makes recursive aliases like
                # `A = Sequence[str | A]` valid here, similar to how we treat base classes in class
                # definitions, allowing `class str(Sequence[str]): ...`
                incomplete_target = isinstance(res, ProperType) and isinstance(
                    res, PlaceholderType
                )
            else:
                incomplete_target = has_placeholder(res)

            if self.found_incomplete_ref(tag) or incomplete_target:
                # Since we have got here, we know this must be a type alias (incomplete refs
                # may appear in nested positions), therefore use becomes_typeinfo=True.
                self.mark_incomplete(s.name.name, s.value, becomes_typeinfo=True)
                return

            self.add_type_alias_deps(depends_on)
            # In addition to the aliases used, we add deps on unbound
            # type variables, since they are erased from target type.
            self.add_type_alias_deps(qualified_tvars)
            # The above are only direct deps on other aliases.
            # For subscripted aliases, type deps from expansion are added in deps.py
            # (because the type is stored).
            check_for_explicit_any(
                res, self.options, self.is_typeshed_stub_file, self.msg, context=s
            )
            # When this type alias gets "inlined", the Any is not explicit anymore,
            # so we need to replace it with non-explicit Anys.
            res = make_any_non_explicit(res)
            if self.options.disallow_any_unimported and has_any_from_unimported_type(res):
                self.msg.unimported_type_becomes_any("Type alias target", res, s)
                res = make_any_non_unimported(res)
            eager = self.is_func_scope()
            if isinstance(res, ProperType) and isinstance(res, Instance):
                fix_instance(res, self.fail, self.note, disallow_any=False, options=self.options)
            alias_node = TypeAlias(
                res,
                self.qualified_name(s.name.name),
                s.line,
                s.column,
                alias_tvars=alias_tvars,
                no_args=False,
                eager=eager,
                python_3_12_type_alias=True,
            )
            s.alias_node = alias_node

            if (
                existing
                and isinstance(existing.node, (PlaceholderNode, TypeAlias))
                and existing.node.line == s.line
            ):
                updated = False
                if isinstance(existing.node, TypeAlias):
                    if existing.node.target != res:
                        # Copy expansion to the existing alias, this matches how we update base classes
                        # for a TypeInfo _in place_ if there are nested placeholders.
                        existing.node.target = res
                        existing.node.alias_tvars = alias_tvars
                        updated = True
                else:
                    # Otherwise just replace existing placeholder with type alias.
                    existing.node = alias_node
                    updated = True

                if updated:
                    if self.final_iteration:
                        self.cannot_resolve_name(s.name.name, "name", s)
                        return
                    else:
                        # We need to defer so that this change can get propagated to base classes.
                        self.defer(s, force_progress=True)
            else:
                self.add_symbol(s.name.name, alias_node, s)

            current_node = existing.node if existing else alias_node
            assert isinstance(current_node, TypeAlias)
            self.disable_invalid_recursive_aliases(s, current_node, s.value)
            s.name.accept(self)
        finally:
            self.pop_type_args(s.type_args)

    #
    # Expressions
    #

    def visit_name_expr(self, expr: NameExpr) -> None:
        n = self.lookup(expr.name, expr)
        if n:
            self.bind_name_expr(expr, n)

    def bind_name_expr(self, expr: NameExpr, sym: SymbolTableNode) -> None:
        """Bind name expression to a symbol table node."""
        if (
            isinstance(sym.node, TypeVarExpr)
            and self.tvar_scope.get_binding(sym)
            and not self.allow_unbound_tvars
        ):
            self.fail(f'"{expr.name}" is a type variable and only valid in type context', expr)
        elif isinstance(sym.node, PlaceholderNode):
            self.process_placeholder(expr.name, "name", expr)
        else:
            expr.kind = sym.kind
            expr.node = sym.node
            expr.fullname = sym.fullname or ""

    def visit_super_expr(self, expr: SuperExpr) -> None:
        if not self.type and not expr.call.args:
            self.fail('"super" used outside class', expr)
            return
        expr.info = self.type
        for arg in expr.call.args:
            arg.accept(self)

    def visit_tuple_expr(self, expr: TupleExpr) -> None:
        for item in expr.items:
            if isinstance(item, StarExpr):
                item.valid = True
            item.accept(self)

    def visit_list_expr(self, expr: ListExpr) -> None:
        for item in expr.items:
            if isinstance(item, StarExpr):
                item.valid = True
            item.accept(self)

    def visit_set_expr(self, expr: SetExpr) -> None:
        for item in expr.items:
            if isinstance(item, StarExpr):
                item.valid = True
            item.accept(self)

    def visit_dict_expr(self, expr: DictExpr) -> None:
        for key, value in expr.items:
            if key is not None:
                key.accept(self)
            value.accept(self)

    def visit_star_expr(self, expr: StarExpr) -> None:
        if not expr.valid:
            self.fail("can't use starred expression here", expr, blocker=True)
        else:
            expr.expr.accept(self)

    def visit_yield_from_expr(self, e: YieldFromExpr) -> None:
        if not self.is_func_scope():
            self.fail('"yield from" outside function', e, serious=True, blocker=True)
        elif self.scope_stack[-1] == SCOPE_COMPREHENSION:
            self.fail(
                '"yield from" inside comprehension or generator expression',
                e,
                serious=True,
                blocker=True,
            )
        elif self.function_stack[-1].is_coroutine:
            self.fail('"yield from" in async function', e, serious=True, blocker=True)
        else:
            self.function_stack[-1].is_generator = True
        if e.expr:
            e.expr.accept(self)

    def visit_call_expr(self, expr: CallExpr) -> None:
        """Analyze a call expression.

        Some call expressions are recognized as special forms, including
        cast(...).
        """
        expr.callee.accept(self)
        if refers_to_fullname(expr.callee, "typing.cast"):
            # Special form cast(...).
            if not self.check_fixed_args(expr, 2, "cast"):
                return
            # Translate first argument to an unanalyzed type.
            try:
                target = self.expr_to_unanalyzed_type(expr.args[0])
            except TypeTranslationError:
                self.fail("Cast target is not a type", expr)
                return
            # Piggyback CastExpr object to the CallExpr object; it takes
            # precedence over the CallExpr semantics.
            expr.analyzed = CastExpr(expr.args[1], target)
            expr.analyzed.line = expr.line
            expr.analyzed.column = expr.column
            expr.analyzed.accept(self)
        elif refers_to_fullname(expr.callee, ASSERT_TYPE_NAMES):
            if not self.check_fixed_args(expr, 2, "assert_type"):
                return
            # Translate second argument to an unanalyzed type.
            try:
                target = self.expr_to_unanalyzed_type(expr.args[1])
            except TypeTranslationError:
                self.fail("assert_type() type is not a type", expr)
                return
            expr.analyzed = AssertTypeExpr(expr.args[0], target)
            expr.analyzed.line = expr.line
            expr.analyzed.column = expr.column
            expr.analyzed.accept(self)
        elif refers_to_fullname(expr.callee, REVEAL_TYPE_NAMES):
            if not self.check_fixed_args(expr, 1, "reveal_type"):
                return
            reveal_imported = False
            reveal_type_node = self.lookup("reveal_type", expr, suppress_errors=True)
            if (
                reveal_type_node
                and isinstance(reveal_type_node.node, SYMBOL_FUNCBASE_TYPES)
                and reveal_type_node.fullname in IMPORTED_REVEAL_TYPE_NAMES
            ):
                reveal_imported = True
            expr.analyzed = RevealExpr(
                kind=REVEAL_TYPE, expr=expr.args[0], is_imported=reveal_imported
            )
            expr.analyzed.line = expr.line
            expr.analyzed.column = expr.column
            expr.analyzed.accept(self)
        elif refers_to_fullname(expr.callee, "builtins.reveal_locals"):
            # Store the local variable names into the RevealExpr for use in the
            # type checking pass
            local_nodes: list[Var] = []
            if self.is_module_scope():
                # try to determine just the variable declarations in module scope
                # self.globals.values() contains SymbolTableNode's
                # Each SymbolTableNode has an attribute node that is nodes.Var
                # look for variable nodes that marked as is_inferred
                # Each symboltable node has a Var node as .node
                local_nodes = [
                    n.node
                    for name, n in self.globals.items()
                    if getattr(n.node, "is_inferred", False) and isinstance(n.node, Var)
                ]
            elif self.is_class_scope():
                # type = None  # type: Optional[TypeInfo]
                if self.type is not None:
                    local_nodes = [
                        st.node for st in self.type.names.values() if isinstance(st.node, Var)
                    ]
            elif self.is_func_scope():
                # locals = None  # type: List[Optional[SymbolTable]]
                if self.locals is not None:
                    symbol_table = self.locals[-1]
                    if symbol_table is not None:
                        local_nodes = [
                            st.node for st in symbol_table.values() if isinstance(st.node, Var)
                        ]
            expr.analyzed = RevealExpr(kind=REVEAL_LOCALS, local_nodes=local_nodes)
            expr.analyzed.line = expr.line
            expr.analyzed.column = expr.column
            expr.analyzed.accept(self)
        elif refers_to_fullname(expr.callee, "typing.Any"):
            # Special form Any(...) no longer supported.
            self.fail("Any(...) is no longer supported. Use cast(Any, ...) instead", expr)
        elif refers_to_fullname(expr.callee, "typing._promote"):
            # Special form _promote(...).
            if not self.check_fixed_args(expr, 1, "_promote"):
                return
            # Translate first argument to an unanalyzed type.
            try:
                target = self.expr_to_unanalyzed_type(expr.args[0])
            except TypeTranslationError:
                self.fail("Argument 1 to _promote is not a type", expr)
                return
            expr.analyzed = PromoteExpr(target)
            expr.analyzed.line = expr.line
            expr.analyzed.accept(self)
        elif refers_to_fullname(expr.callee, "builtins.dict"):
            expr.analyzed = self.translate_dict_call(expr)
        elif refers_to_fullname(expr.callee, "builtins.divmod"):
            if not self.check_fixed_args(expr, 2, "divmod"):
                return
            expr.analyzed = OpExpr("divmod", expr.args[0], expr.args[1])
            expr.analyzed.line = expr.line
            expr.analyzed.accept(self)
        elif refers_to_fullname(
            expr.callee, ("typing.TypeAliasType", "typing_extensions.TypeAliasType")
        ):
            with self.allow_unbound_tvars_set():
                for a in expr.args:
                    a.accept(self)
        else:
            # Normal call expression.
            for a in expr.args:
                a.accept(self)

            if (
                isinstance(expr.callee, MemberExpr)
                and isinstance(expr.callee.expr, NameExpr)
                and expr.callee.expr.name == "__all__"
                and expr.callee.expr.kind == GDEF
                and expr.callee.name in ("append", "extend", "remove")
            ):
                if expr.callee.name == "append" and expr.args:
                    self.add_exports(expr.args[0])
                elif (
                    expr.callee.name == "extend"
                    and expr.args
                    and isinstance(expr.args[0], (ListExpr, TupleExpr))
                ):
                    self.add_exports(expr.args[0].items)
                elif (
                    expr.callee.name == "remove"
                    and expr.args
                    and isinstance(expr.args[0], StrExpr)
                ):
                    self.all_exports = [n for n in self.all_exports if n != expr.args[0].value]

    def translate_dict_call(self, call: CallExpr) -> DictExpr | None:
        """Translate 'dict(x=y, ...)' to {'x': y, ...} and 'dict()' to {}.

        For other variants of dict(...), return None.
        """
        if not all(kind in (ARG_NAMED, ARG_STAR2) for kind in call.arg_kinds):
            # Must still accept those args.
            for a in call.args:
                a.accept(self)
            return None
        expr = DictExpr(
            [
                (StrExpr(key) if key is not None else None, value)
                for key, value in zip(call.arg_names, call.args)
            ]
        )
        expr.set_line(call)
        expr.accept(self)
        return expr

    def check_fixed_args(self, expr: CallExpr, numargs: int, name: str) -> bool:
        """Verify that expr has specified number of positional args.

        Return True if the arguments are valid.
        """
        s = "s"
        if numargs == 1:
            s = ""
        if len(expr.args) != numargs:
            self.fail('"%s" expects %d argument%s' % (name, numargs, s), expr)
            return False
        if expr.arg_kinds != [ARG_POS] * numargs:
            self.fail(f'"{name}" must be called with {numargs} positional argument{s}', expr)
            return False
        return True

    def visit_member_expr(self, expr: MemberExpr) -> None:
        base = expr.expr
        base.accept(self)
        if isinstance(base, RefExpr) and isinstance(base.node, MypyFile):
            # Handle module attribute.
            sym = self.get_module_symbol(base.node, expr.name)
            if sym:
                if isinstance(sym.node, PlaceholderNode):
                    self.process_placeholder(expr.name, "attribute", expr)
                    return
                expr.kind = sym.kind
                expr.fullname = sym.fullname or ""
                expr.node = sym.node
        elif isinstance(base, RefExpr):
            # This branch handles the case C.bar (or cls.bar or self.bar inside
            # a classmethod/method), where C is a class and bar is a type
            # definition or a module resulting from `import bar` (or a module
            # assignment) inside class C. We look up bar in the class' TypeInfo
            # namespace.  This is done only when bar is a module or a type;
            # other things (e.g. methods) are handled by other code in
            # checkmember.
            type_info = None
            if isinstance(base.node, TypeInfo):
                # C.bar where C is a class
                type_info = base.node
            elif isinstance(base.node, Var) and self.type and self.function_stack:
                # check for self.bar or cls.bar in method/classmethod
                func_def = self.function_stack[-1]
                if not func_def.is_static and isinstance(func_def.type, CallableType):
                    formal_arg = func_def.type.argument_by_name(base.node.name)
                    if formal_arg and formal_arg.pos == 0:
                        type_info = self.type
            elif isinstance(base.node, TypeAlias) and base.node.no_args:
                assert isinstance(base.node.target, ProperType)
                if isinstance(base.node.target, Instance):
                    type_info = base.node.target.type

            if type_info:
                n = type_info.names.get(expr.name)
                if n is not None and isinstance(n.node, (MypyFile, TypeInfo, TypeAlias)):
                    if not n:
                        return
                    expr.kind = n.kind
                    expr.fullname = n.fullname or ""
                    expr.node = n.node

    def visit_op_expr(self, expr: OpExpr) -> None:
        expr.left.accept(self)

        if expr.op in ("and", "or"):
            inferred = infer_condition_value(expr.left, self.options)
            if (inferred in (ALWAYS_FALSE, MYPY_FALSE) and expr.op == "and") or (
                inferred in (ALWAYS_TRUE, MYPY_TRUE) and expr.op == "or"
            ):
                expr.right_unreachable = True
                return
            elif (inferred in (ALWAYS_TRUE, MYPY_TRUE) and expr.op == "and") or (
                inferred in (ALWAYS_FALSE, MYPY_FALSE) and expr.op == "or"
            ):
                expr.right_always = True

        expr.right.accept(self)

    def visit_comparison_expr(self, expr: ComparisonExpr) -> None:
        for operand in expr.operands:
            operand.accept(self)

    def visit_unary_expr(self, expr: UnaryExpr) -> None:
        expr.expr.accept(self)

    def visit_index_expr(self, expr: IndexExpr) -> None:
        base = expr.base
        base.accept(self)
        if (
            isinstance(base, RefExpr)
            and isinstance(base.node, TypeInfo)
            and not base.node.is_generic()
        ):
            expr.index.accept(self)
        elif (
            isinstance(base, RefExpr) and isinstance(base.node, TypeAlias)
        ) or refers_to_class_or_function(base):
            # We need to do full processing on every iteration, since some type
            # arguments may contain placeholder types.
            self.analyze_type_application(expr)
        else:
            expr.index.accept(self)

    def analyze_type_application(self, expr: IndexExpr) -> None:
        """Analyze special form -- type application (either direct or via type aliasing)."""
        types = self.analyze_type_application_args(expr)
        if types is None:
            return
        base = expr.base
        expr.analyzed = TypeApplication(base, types)
        expr.analyzed.line = expr.line
        expr.analyzed.column = expr.column

    def analyze_type_application_args(self, expr: IndexExpr) -> list[Type] | None:
        """Analyze type arguments (index) in a type application.

        Return None if anything was incomplete.
        """
        index = expr.index
        tag = self.track_incomplete_refs()
        self.analyze_type_expr(index)
        if self.found_incomplete_ref(tag):
            return None
        if self.basic_type_applications:
            # Postpone the rest until we have more information (for r.h.s. of an assignment)
            return None
        types: list[Type] = []
        if isinstance(index, TupleExpr):
            items = index.items
            is_tuple = isinstance(expr.base, RefExpr) and expr.base.fullname == "builtins.tuple"
            if is_tuple and len(items) == 2 and isinstance(items[-1], EllipsisExpr):
                items = items[:-1]
        else:
            items = [index]

        # TODO: this needs a clean-up.
        # Probably always allow Parameters literals, and validate in semanal_typeargs.py
        base = expr.base
        if isinstance(base, RefExpr) and isinstance(base.node, TypeAlias):
            allow_unpack = base.node.tvar_tuple_index is not None
            alias = base.node
            if any(isinstance(t, ParamSpecType) for t in alias.alias_tvars):
                has_param_spec = True
                num_args = len(alias.alias_tvars)
            else:
                has_param_spec = False
                num_args = -1
        elif isinstance(base, RefExpr) and isinstance(base.node, TypeInfo):
            allow_unpack = (
                base.node.has_type_var_tuple_type or base.node.fullname == "builtins.tuple"
            )
            has_param_spec = base.node.has_param_spec_type
            num_args = len(base.node.type_vars)
        else:
            allow_unpack = False
            has_param_spec = False
            num_args = -1

        for item in items:
            try:
                typearg = self.expr_to_unanalyzed_type(item, allow_unpack=True)
            except TypeTranslationError:
                self.fail("Type expected within [...]", expr)
                return None
            analyzed = self.anal_type(
                typearg,
                # The type application may appear in base class expression,
                # where type variables are not bound yet. Or when accepting
                # r.h.s. of type alias before we figured out it is a type alias.
                allow_unbound_tvars=self.allow_unbound_tvars,
                allow_placeholder=True,
                allow_param_spec_literals=has_param_spec,
                allow_unpack=allow_unpack,
            )
            if analyzed is None:
                return None
            types.append(analyzed)

        if allow_unpack:
            types = self.type_analyzer().check_unpacks_in_list(types)
        if has_param_spec and num_args == 1 and types:
            first_arg = get_proper_type(types[0])
            single_any = len(types) == 1 and isinstance(first_arg, AnyType)
            if not (single_any or any(isinstance(t, (Parameters, ParamSpecType)) for t in types)):
                types = [Parameters(types, [ARG_POS] * len(types), [None] * len(types))]

        return types

    def visit_slice_expr(self, expr: SliceExpr) -> None:
        if expr.begin_index:
            expr.begin_index.accept(self)
        if expr.end_index:
            expr.end_index.accept(self)
        if expr.stride:
            expr.stride.accept(self)

    def visit_cast_expr(self, expr: CastExpr) -> None:
        expr.expr.accept(self)
        analyzed = self.anal_type(expr.type)
        if analyzed is not None:
            expr.type = analyzed

    def visit_assert_type_expr(self, expr: AssertTypeExpr) -> None:
        expr.expr.accept(self)
        analyzed = self.anal_type(expr.type)
        if analyzed is not None:
            expr.type = analyzed

    def visit_reveal_expr(self, expr: RevealExpr) -> None:
        if expr.kind == REVEAL_TYPE:
            if expr.expr is not None:
                expr.expr.accept(self)
        else:
            # Reveal locals doesn't have an inner expression, there's no
            # need to traverse inside it
            pass

    def visit_type_application(self, expr: TypeApplication) -> None:
        expr.expr.accept(self)
        for i in range(len(expr.types)):
            analyzed = self.anal_type(expr.types[i])
            if analyzed is not None:
                expr.types[i] = analyzed

    def visit_list_comprehension(self, expr: ListComprehension) -> None:
        if any(expr.generator.is_async):
            if not self.is_func_scope() or not self.function_stack[-1].is_coroutine:
                self.fail(message_registry.ASYNC_FOR_OUTSIDE_COROUTINE, expr, code=codes.SYNTAX)

        expr.generator.accept(self)

    def visit_set_comprehension(self, expr: SetComprehension) -> None:
        if any(expr.generator.is_async):
            if not self.is_func_scope() or not self.function_stack[-1].is_coroutine:
                self.fail(message_registry.ASYNC_FOR_OUTSIDE_COROUTINE, expr, code=codes.SYNTAX)

        expr.generator.accept(self)

    def visit_dictionary_comprehension(self, expr: DictionaryComprehension) -> None:
        if any(expr.is_async):
            if not self.is_func_scope() or not self.function_stack[-1].is_coroutine:
                self.fail(message_registry.ASYNC_FOR_OUTSIDE_COROUTINE, expr, code=codes.SYNTAX)

        with self.enter(expr):
            self.analyze_comp_for(expr)
            expr.key.accept(self)
            expr.value.accept(self)
        self.analyze_comp_for_2(expr)

    def visit_generator_expr(self, expr: GeneratorExpr) -> None:
        with self.enter(expr):
            self.analyze_comp_for(expr)
            expr.left_expr.accept(self)
        self.analyze_comp_for_2(expr)

    def analyze_comp_for(self, expr: GeneratorExpr | DictionaryComprehension) -> None:
        """Analyses the 'comp_for' part of comprehensions (part 1).

        That is the part after 'for' in (x for x in l if p). This analyzes
        variables and conditions which are analyzed in a local scope.
        """
        for i, (index, sequence, conditions) in enumerate(
            zip(expr.indices, expr.sequences, expr.condlists)
        ):
            if i > 0:
                sequence.accept(self)
            # Bind index variables.
            self.analyze_lvalue(index)
            for cond in conditions:
                cond.accept(self)

    def analyze_comp_for_2(self, expr: GeneratorExpr | DictionaryComprehension) -> None:
        """Analyses the 'comp_for' part of comprehensions (part 2).

        That is the part after 'for' in (x for x in l if p). This analyzes
        the 'l' part which is analyzed in the surrounding scope.
        """
        expr.sequences[0].accept(self)

    def visit_lambda_expr(self, expr: LambdaExpr) -> None:
        self.analyze_arg_initializers(expr)
        with self.inside_except_star_block_set(False, entering_loop=False):
            self.analyze_function_body(expr)

    def visit_conditional_expr(self, expr: ConditionalExpr) -> None:
        expr.if_expr.accept(self)
        expr.cond.accept(self)
        expr.else_expr.accept(self)

    def visit__promote_expr(self, expr: PromoteExpr) -> None:
        analyzed = self.anal_type(expr.type)
        if analyzed is not None:
            assert isinstance(analyzed, ProperType), "Cannot use type aliases for promotions"
            expr.type = analyzed

    def visit_yield_expr(self, e: YieldExpr) -> None:
        if not self.is_func_scope():
            self.fail('"yield" outside function', e, serious=True, blocker=True)
        elif self.scope_stack[-1] == SCOPE_COMPREHENSION:
            self.fail(
                '"yield" inside comprehension or generator expression',
                e,
                serious=True,
                blocker=True,
            )
        elif self.function_stack[-1].is_coroutine:
            self.function_stack[-1].is_generator = True
            self.function_stack[-1].is_async_generator = True
        else:
            self.function_stack[-1].is_generator = True
        if e.expr:
            e.expr.accept(self)

    def visit_await_expr(self, expr: AwaitExpr) -> None:
        if not self.is_func_scope() or not self.function_stack:
            # We check both because is_function_scope() returns True inside comprehensions.
            # This is not a blocker, because some environments (like ipython)
            # support top level awaits.
            self.fail('"await" outside function', expr, serious=True, code=codes.TOP_LEVEL_AWAIT)
        elif not self.function_stack[-1].is_coroutine:
            self.fail(
                '"await" outside coroutine ("async def")',
                expr,
                serious=True,
                code=codes.AWAIT_NOT_ASYNC,
            )
        expr.expr.accept(self)

    #
    # Patterns
    #

    def visit_as_pattern(self, p: AsPattern) -> None:
        if p.pattern is not None:
            p.pattern.accept(self)
        if p.name is not None:
            self.analyze_lvalue(p.name)

    def visit_or_pattern(self, p: OrPattern) -> None:
        for pattern in p.patterns:
            pattern.accept(self)

    def visit_value_pattern(self, p: ValuePattern) -> None:
        p.expr.accept(self)

    def visit_sequence_pattern(self, p: SequencePattern) -> None:
        for pattern in p.patterns:
            pattern.accept(self)

    def visit_starred_pattern(self, p: StarredPattern) -> None:
        if p.capture is not None:
            self.analyze_lvalue(p.capture)

    def visit_mapping_pattern(self, p: MappingPattern) -> None:
        for key in p.keys:
            key.accept(self)
        for value in p.values:
            value.accept(self)
        if p.rest is not None:
            self.analyze_lvalue(p.rest)

    def visit_class_pattern(self, p: ClassPattern) -> None:
        p.class_ref.accept(self)
        for pos in p.positionals:
            pos.accept(self)
        for v in p.keyword_values:
            v.accept(self)

    #
    # Lookup functions
    #

    def lookup(
        self, name: str, ctx: Context, suppress_errors: bool = False
    ) -> SymbolTableNode | None:
        """Look up an unqualified (no dots) name in all active namespaces.

        Note that the result may contain a PlaceholderNode. The caller may
        want to defer in that case.

        Generate an error if the name is not defined unless suppress_errors
        is true or the current namespace is incomplete. In the latter case
        defer.
        """
        implicit_name = False
        # 1a. Name declared using 'global x' takes precedence
        if name in self.global_decls[-1]:
            if name in self.globals:
                return self.globals[name]
            if not suppress_errors:
                self.name_not_defined(name, ctx)
            return None
        # 1b. Name declared using 'nonlocal x' takes precedence
        if name in self.nonlocal_decls[-1]:
            for table in reversed(self.locals[:-1]):
                if table is not None and name in table:
                    return table[name]
            if not suppress_errors:
                self.name_not_defined(name, ctx)
            return None
        # 2a. Class attributes (if within class definition)
        if self.type and not self.is_func_scope() and name in self.type.names:
            node = self.type.names[name]
            if not node.implicit:
                if self.is_active_symbol_in_class_body(node.node):
                    return node
            else:
                # Defined through self.x assignment
                implicit_name = True
                implicit_node = node
        # 2b. Class attributes __qualname__ and __module__
        if self.type and not self.is_func_scope() and name in {"__qualname__", "__module__"}:
            return SymbolTableNode(MDEF, Var(name, self.str_type()))
        # 3. Local (function) scopes
        for table in reversed(self.locals):
            if table is not None and name in table:
                return table[name]

        # 4. Current file global scope
        if name in self.globals:
            return self.globals[name]
        # 5. Builtins
        b = self.globals.get("__builtins__", None)
        if b:
            assert isinstance(b.node, MypyFile)
            table = b.node.names
            if name in table:
                if len(name) > 1 and name[0] == "_" and name[1] != "_":
                    if not suppress_errors:
                        self.name_not_defined(name, ctx)
                    return None
                node = table[name]
                return node
        # Give up.
        if not implicit_name and not suppress_errors:
            self.name_not_defined(name, ctx)
        else:
            if implicit_name:
                return implicit_node
        return None

    def is_active_symbol_in_class_body(self, node: SymbolNode | None) -> bool:
        """Can a symbol defined in class body accessed at current statement?

        Only allow access to class attributes textually after
        the definition, so that it's possible to fall back to the
        outer scope. Example:

            class X: ...

            class C:
                X = X  # Initializer refers to outer scope

        Nested classes are an exception, since we want to support
        arbitrary forward references in type annotations. Also, we
        allow forward references to type aliases to support recursive
        types.
        """
        # TODO: Forward reference to name imported in class body is not
        #       caught.
        if self.statement is None:
            # Assume it's fine -- don't have enough context to check
            return True
        if (
            node is None
            or self.is_textually_before_statement(node)
            or not self.is_defined_in_current_module(node.fullname)
        ):
            return True
        if self.is_type_like(node):
            # Allow forward references to classes/type aliases (see docstring), but
            # a forward reference should never shadow an existing regular reference.
            if node.name not in self.globals:
                return True
            global_node = self.globals[node.name]
            if not self.is_textually_before_class(global_node.node):
                return True
            return not self.is_type_like(global_node.node)
        return False

    def is_type_like(self, node: SymbolNode | None) -> bool:
        return isinstance(node, (TypeInfo, TypeAlias)) or (
            isinstance(node, PlaceholderNode) and node.becomes_typeinfo
        )

    def is_textually_before_statement(self, node: SymbolNode) -> bool:
        """Check if a node is defined textually before the current statement

        Note that decorated functions' line number are the same as
        the top decorator.
        """
        assert self.statement
        line_diff = self.statement.line - node.line

        # The first branch handles reference an overloaded function variant inside itself,
        # this is a corner case where mypy technically deviates from runtime name resolution,
        # but it is fine because we want an overloaded function to be treated as a single unit.
        if self.is_overloaded_item(node, self.statement):
            return False
        elif isinstance(node, Decorator) and not node.is_overload:
            return line_diff > len(node.original_decorators)
        else:
            return line_diff > 0

    def is_textually_before_class(self, node: SymbolNode | None) -> bool:
        """Similar to above, but check if a node is defined before current class."""
        assert self.type is not None
        if node is None:
            return False
        return node.line < self.type.defn.line

    def is_overloaded_item(self, node: SymbolNode, statement: Statement) -> bool:
        """Check whether the function belongs to the overloaded variants"""
        if isinstance(node, OverloadedFuncDef) and isinstance(statement, FuncDef):
            in_items = statement in {
                item.func if isinstance(item, Decorator) else item for item in node.items
            }
            in_impl = node.impl is not None and (
                (isinstance(node.impl, Decorator) and statement is node.impl.func)
                or statement is node.impl
            )
            return in_items or in_impl
        return False

    def is_defined_in_current_module(self, fullname: str | None) -> bool:
        if not fullname:
            return False
        return module_prefix(self.modules, fullname) == self.cur_mod_id

    def lookup_qualified(
        self, name: str, ctx: Context, suppress_errors: bool = False
    ) -> SymbolTableNode | None:
        """Lookup a qualified name in all activate namespaces.

        Note that the result may contain a PlaceholderNode. The caller may
        want to defer in that case.

        Generate an error if the name is not defined unless suppress_errors
        is true or the current namespace is incomplete. In the latter case
        defer.
        """
        if "." not in name:
            # Simple case: look up a short name.
            return self.lookup(name, ctx, suppress_errors=suppress_errors)
        parts = name.split(".")
        namespace = self.cur_mod_id
        sym = self.lookup(parts[0], ctx, suppress_errors=suppress_errors)
        if sym:
            for i in range(1, len(parts)):
                node = sym.node
                part = parts[i]
                if isinstance(node, TypeInfo):
                    nextsym = node.get(part)
                elif isinstance(node, MypyFile):
                    nextsym = self.get_module_symbol(node, part)
                    namespace = node.fullname
                elif isinstance(node, PlaceholderNode):
                    return sym
                elif isinstance(node, TypeAlias) and node.no_args:
                    assert isinstance(node.target, ProperType)
                    if isinstance(node.target, Instance):
                        nextsym = node.target.type.get(part)
                    else:
                        nextsym = None
                else:
                    if isinstance(node, Var):
                        typ = get_proper_type(node.type)
                        if isinstance(typ, AnyType):
                            # Allow access through Var with Any type without error.
                            return self.implicit_symbol(sym, name, parts[i:], typ)
                    # This might be something like valid `P.args` or invalid `P.__bound__` access.
                    # Important note that `ParamSpecExpr` is also ignored in other places.
                    # See https://github.com/python/mypy/pull/13468
                    if isinstance(node, ParamSpecExpr) and part in ("args", "kwargs"):
                        return None
                    # Lookup through invalid node, such as variable or function
                    nextsym = None
                if not nextsym or nextsym.module_hidden:
                    if not suppress_errors:
                        self.name_not_defined(name, ctx, namespace=namespace)
                    return None
                sym = nextsym
        return sym

    def lookup_type_node(self, expr: Expression) -> SymbolTableNode | None:
        try:
            t = self.expr_to_unanalyzed_type(expr)
        except TypeTranslationError:
            return None
        if isinstance(t, UnboundType):
            n = self.lookup_qualified(t.name, expr, suppress_errors=True)
            return n
        return None

    def get_module_symbol(self, node: MypyFile, name: str) -> SymbolTableNode | None:
        """Look up a symbol from a module.

        Return None if no matching symbol could be bound.
        """
        module = node.fullname
        names = node.names
        sym = names.get(name)
        if not sym:
            fullname = module + "." + name
            if fullname in self.modules:
                sym = SymbolTableNode(GDEF, self.modules[fullname])
            elif self.is_incomplete_namespace(module):
                self.record_incomplete_ref()
            elif "__getattr__" in names:
                gvar = self.create_getattr_var(names["__getattr__"], name, fullname)
                if gvar:
                    sym = SymbolTableNode(GDEF, gvar)
            elif self.is_missing_module(fullname):
                # We use the fullname of the original definition so that we can
                # detect whether two names refer to the same thing.
                var_type = AnyType(TypeOfAny.from_unimported_type)
                v = Var(name, type=var_type)
                v._fullname = fullname
                sym = SymbolTableNode(GDEF, v)
        elif sym.module_hidden:
            sym = None
        return sym

    def is_missing_module(self, module: str) -> bool:
        return module in self.missing_modules

    def implicit_symbol(
        self, sym: SymbolTableNode, name: str, parts: list[str], source_type: AnyType
    ) -> SymbolTableNode:
        """Create symbol for a qualified name reference through Any type."""
        if sym.node is None:
            basename = None
        else:
            basename = sym.node.fullname
        if basename is None:
            fullname = name
        else:
            fullname = basename + "." + ".".join(parts)
        var_type = AnyType(TypeOfAny.from_another_any, source_type)
        var = Var(parts[-1], var_type)
        var._fullname = fullname
        return SymbolTableNode(GDEF, var)

    def create_getattr_var(
        self, getattr_defn: SymbolTableNode, name: str, fullname: str
    ) -> Var | None:
        """Create a dummy variable using module-level __getattr__ return type.

        If not possible, return None.

        Note that multiple Var nodes can be created for a single name. We
        can use the from_module_getattr and the fullname attributes to
        check if two dummy Var nodes refer to the same thing. Reusing Var
        nodes would require non-local mutable state, which we prefer to
        avoid.
        """
        if isinstance(getattr_defn.node, (FuncDef, Var)):
            node_type = get_proper_type(getattr_defn.node.type)
            if isinstance(node_type, CallableType):
                typ = node_type.ret_type
            else:
                typ = AnyType(TypeOfAny.from_error)
            v = Var(name, type=typ)
            v._fullname = fullname
            v.from_module_getattr = True
            return v
        return None

    def lookup_fully_qualified(self, fullname: str) -> SymbolTableNode:
        ret = self.lookup_fully_qualified_or_none(fullname)
        assert ret is not None, fullname
        return ret

    def lookup_fully_qualified_or_none(self, fullname: str) -> SymbolTableNode | None:
        """Lookup a fully qualified name that refers to a module-level definition.

        Don't assume that the name is defined. This happens in the global namespace --
        the local module namespace is ignored. This does not dereference indirect
        refs.

        Note that this can't be used for names nested in class namespaces.
        """
        # TODO: unify/clean-up/simplify lookup methods, see #4157.
        module, name = fullname.rsplit(".", maxsplit=1)

        if module in self.modules:
            # If the module exists, look up the name in the module.
            # This is the common case.
            filenode = self.modules[module]
            result = filenode.names.get(name)
            if result is None and self.is_incomplete_namespace(module):
                # TODO: More explicit handling of incomplete refs?
                self.record_incomplete_ref()
            return result
        else:
            # Else, try to find the longest prefix of the module name that is in the modules dictionary.
            splitted_modules = fullname.split(".")
            names = []

            while splitted_modules and ".".join(splitted_modules) not in self.modules:
                names.append(splitted_modules.pop())

            if not splitted_modules or not names:
                # If no module or name is found, return None.
                return None

            # Reverse the names list to get the correct order of names.
            names.reverse()

            module = ".".join(splitted_modules)
            filenode = self.modules[module]
            result = filenode.names.get(names[0])

            if result is None and self.is_incomplete_namespace(module):
                # TODO: More explicit handling of incomplete refs?
                self.record_incomplete_ref()

            for part in names[1:]:
                if result is not None and isinstance(result.node, TypeInfo):
                    result = result.node.names.get(part)
                else:
                    return None
            return result

    def object_type(self) -> Instance:
        return self.named_type("builtins.object")

    def str_type(self) -> Instance:
        return self.named_type("builtins.str")

    def named_type(self, fullname: str, args: list[Type] | None = None) -> Instance:
        sym = self.lookup_fully_qualified(fullname)
        assert sym, "Internal error: attempted to construct unknown type"
        node = sym.node
        assert isinstance(node, TypeInfo)
        if args:
            # TODO: assert len(args) == len(node.defn.type_vars)
            return Instance(node, args)
        return Instance(node, [AnyType(TypeOfAny.special_form)] * len(node.defn.type_vars))

    def named_type_or_none(self, fullname: str, args: list[Type] | None = None) -> Instance | None:
        sym = self.lookup_fully_qualified_or_none(fullname)
        if not sym or isinstance(sym.node, PlaceholderNode):
            return None
        node = sym.node
        if isinstance(node, TypeAlias):
            assert isinstance(node.target, Instance)  # type: ignore[misc]
            node = node.target.type
        assert isinstance(node, TypeInfo), node
        if args is not None:
            # TODO: assert len(args) == len(node.defn.type_vars)
            return Instance(node, args)
        return Instance(node, [AnyType(TypeOfAny.unannotated)] * len(node.defn.type_vars))

    def builtin_type(self, fully_qualified_name: str) -> Instance:
        """Legacy function -- use named_type() instead."""
        return self.named_type(fully_qualified_name)

    def lookup_current_scope(self, name: str) -> SymbolTableNode | None:
        if self.locals[-1] is not None:
            return self.locals[-1].get(name)
        elif self.type is not None:
            return self.type.names.get(name)
        else:
            return self.globals.get(name)

    #
    # Adding symbols
    #

    def add_symbol(
        self,
        name: str,
        node: SymbolNode,
        context: Context,
        module_public: bool = True,
        module_hidden: bool = False,
        can_defer: bool = True,
        escape_comprehensions: bool = False,
        no_progress: bool = False,
        type_param: bool = False,
    ) -> bool:
        """Add symbol to the currently active symbol table.

        Generally additions to symbol table should go through this method or
        one of the methods below so that kinds, redefinitions, conditional
        definitions, and skipped names are handled consistently.

        Return True if we actually added the symbol, or False if we refused to do so
        (because something is not ready).

        If can_defer is True, defer current target if adding a placeholder.
        """
        if self.is_func_scope():
            kind = LDEF
        elif self.type is not None:
            kind = MDEF
        else:
            kind = GDEF
        symbol = SymbolTableNode(
            kind, node, module_public=module_public, module_hidden=module_hidden
        )
        return self.add_symbol_table_node(
            name, symbol, context, can_defer, escape_comprehensions, no_progress, type_param
        )

    def add_symbol_skip_local(self, name: str, node: SymbolNode) -> None:
        """Same as above, but skipping the local namespace.

        This doesn't check for previous definition and is only used
        for serialization of method-level classes.

        Classes defined within methods can be exposed through an
        attribute type, but method-level symbol tables aren't serialized.
        This method can be used to add such classes to an enclosing,
        serialized symbol table.
        """
        # TODO: currently this is only used by named tuples and typed dicts.
        # Use this method also by normal classes, see issue #6422.
        if self.type is not None:
            names = self.type.names
            kind = MDEF
        else:
            names = self.globals
            kind = GDEF
        symbol = SymbolTableNode(kind, node)
        names[name] = symbol

    def add_symbol_table_node(
        self,
        name: str,
        symbol: SymbolTableNode,
        context: Context | None = None,
        can_defer: bool = True,
        escape_comprehensions: bool = False,
        no_progress: bool = False,
        type_param: bool = False,
    ) -> bool:
        """Add symbol table node to the currently active symbol table.

        Return True if we actually added the symbol, or False if we refused
        to do so (because something is not ready or it was a no-op).

        Generate an error if there is an invalid redefinition.

        If context is None, unconditionally add node, since we can't report
        an error. Note that this is used by plugins to forcibly replace nodes!

        TODO: Prevent plugins from replacing nodes, as it could cause problems?

        Args:
            name: short name of symbol
            symbol: Node to add
            can_defer: if True, defer current target if adding a placeholder
            context: error context (see above about None value)
        """
        names = self.current_symbol_table(
            escape_comprehensions=escape_comprehensions, type_param=type_param
        )
        existing = names.get(name)
        if isinstance(symbol.node, PlaceholderNode) and can_defer:
            if context is not None:
                self.process_placeholder(name, "name", context)
            else:
                # see note in docstring describing None contexts
                self.defer()
        if (
            existing is not None
            and context is not None
            and not is_valid_replacement(existing, symbol)
        ):
            # There is an existing node, so this may be a redefinition.
            # If the new node points to the same node as the old one,
            # or if both old and new nodes are placeholders, we don't
            # need to do anything.
            old = existing.node
            new = symbol.node
            if isinstance(new, PlaceholderNode):
                # We don't know whether this is okay. Let's wait until the next iteration.
                return False
            if not is_same_symbol(old, new):
                if isinstance(new, (FuncDef, Decorator, OverloadedFuncDef, TypeInfo)):
                    self.add_redefinition(names, name, symbol)
                if not (isinstance(new, (FuncDef, Decorator)) and self.set_original_def(old, new)):
                    self.name_already_defined(name, context, existing)
        elif name not in self.missing_names[-1] and "*" not in self.missing_names[-1]:
            names[name] = symbol
            if not no_progress:
                self.progress = True
            return True
        return False

    def add_redefinition(self, names: SymbolTable, name: str, symbol: SymbolTableNode) -> None:
        """Add a symbol table node that reflects a redefinition as a function or a class.

        Redefinitions need to be added to the symbol table so that they can be found
        through AST traversal, but they have dummy names of form 'name-redefinition[N]',
        where N ranges over 2, 3, ... (omitted for the first redefinition).

        Note: we always store redefinitions independently of whether they are valid or not
        (so they will be semantically analyzed), the caller should give an error for invalid
        redefinitions (such as e.g. variable redefined as a class).
        """
        i = 1
        # Don't serialize redefined nodes. They are likely to have
        # busted internal references which can cause problems with
        # serialization and they can't have any external references to
        # them.
        symbol.no_serialize = True
        while True:
            if i == 1:
                new_name = f"{name}-redefinition"
            else:
                new_name = f"{name}-redefinition{i}"
            existing = names.get(new_name)
            if existing is None:
                names[new_name] = symbol
                return
            elif existing.node is symbol.node:
                # Already there
                return
            i += 1

    def add_local(self, node: Var | FuncDef | OverloadedFuncDef, context: Context) -> None:
        """Add local variable or function."""
        assert self.is_func_scope()
        name = node.name
        node._fullname = name
        self.add_symbol(name, node, context)

    def _get_node_for_class_scoped_import(
        self, name: str, symbol_node: SymbolNode | None, context: Context
    ) -> SymbolNode | None:
        if symbol_node is None:
            return None
        # I promise this type checks; I'm just making mypyc issues go away.
        # mypyc is absolutely convinced that `symbol_node` narrows to a Var in the following,
        # when it can also be a FuncBase. Once fixed, `f` in the following can be removed.
        # See also https://github.com/mypyc/mypyc/issues/892
        f: Callable[[object], Any] = lambda x: x
        if isinstance(f(symbol_node), (Decorator, FuncBase, Var)):
            # For imports in class scope, we construct a new node to represent the symbol and
            # set its `info` attribute to `self.type`.
            existing = self.current_symbol_table().get(name)
            if (
                # The redefinition checks in `add_symbol_table_node` don't work for our
                # constructed Var / FuncBase, so check for possible redefinitions here.
                existing is not None
                and isinstance(f(existing.node), (Decorator, FuncBase, Var))
                and (
                    isinstance(f(existing.type), f(AnyType))
                    or f(existing.type) == f(symbol_node).type
                )
            ):
                return existing.node

            # Construct the new node
            if isinstance(f(symbol_node), (FuncBase, Decorator)):
                # In theory we could construct a new node here as well, but in practice
                # it doesn't work well, see #12197
                typ: Type | None = AnyType(TypeOfAny.from_error)
                self.fail("Unsupported class scoped import", context)
            else:
                typ = f(symbol_node).type
            symbol_node = Var(name, typ)
            symbol_node._fullname = self.qualified_name(name)
            assert self.type is not None  # guaranteed by is_class_scope
            symbol_node.info = self.type
            symbol_node.line = context.line
            symbol_node.column = context.column
        return symbol_node

    def add_imported_symbol(
        self,
        name: str,
        node: SymbolTableNode,
        context: ImportBase,
        module_public: bool,
        module_hidden: bool,
    ) -> None:
        """Add an alias to an existing symbol through import."""
        assert not module_hidden or not module_public

        existing_symbol = self.lookup_current_scope(name)
        if (
            existing_symbol
            and not isinstance(existing_symbol.node, PlaceholderNode)
            and not isinstance(node.node, PlaceholderNode)
        ):
            # Import can redefine a variable. They get special treatment.
            if self.process_import_over_existing_name(name, existing_symbol, node, context):
                return

        symbol_node: SymbolNode | None = node.node

        if self.is_class_scope():
            symbol_node = self._get_node_for_class_scoped_import(name, symbol_node, context)

        symbol = SymbolTableNode(
            node.kind, symbol_node, module_public=module_public, module_hidden=module_hidden
        )
        self.add_symbol_table_node(name, symbol, context)

    def add_unknown_imported_symbol(
        self,
        name: str,
        context: Context,
        target_name: str | None,
        module_public: bool,
        module_hidden: bool,
    ) -> None:
        """Add symbol that we don't know what it points to because resolving an import failed.

        This can happen if a module is missing, or it is present, but doesn't have
        the imported attribute. The `target_name` is the name of symbol in the namespace
        it is imported from. For example, for 'from mod import x as y' the target_name is
        'mod.x'. This is currently used only to track logical dependencies.
        """
        existing = self.current_symbol_table().get(name)
        if existing and isinstance(existing.node, Var) and existing.node.is_suppressed_import:
            # This missing import was already added -- nothing to do here.
            return
        var = Var(name)
        if self.options.logical_deps and target_name is not None:
            # This makes it possible to add logical fine-grained dependencies
            # from a missing module. We can't use this by default, since in a
            # few places we assume that the full name points to a real
            # definition, but this name may point to nothing.
            var._fullname = target_name
        elif self.type:
            var._fullname = self.type.fullname + "." + name
            var.info = self.type
        else:
            var._fullname = self.qualified_name(name)
        var.is_ready = True
        any_type = AnyType(TypeOfAny.from_unimported_type, missing_import_name=var._fullname)
        var.type = any_type
        var.is_suppressed_import = True
        self.add_symbol(
            name, var, context, module_public=module_public, module_hidden=module_hidden
        )

    #
    # Other helpers
    #

    @contextmanager
    def tvar_scope_frame(self, frame: TypeVarLikeScope) -> Iterator[None]:
        old_scope = self.tvar_scope
        self.tvar_scope = frame
        yield
        self.tvar_scope = old_scope

    def defer(self, debug_context: Context | None = None, force_progress: bool = False) -> None:
        """Defer current analysis target to be analyzed again.

        This must be called if something in the current target is
        incomplete or has a placeholder node. However, this must *not*
        be called during the final analysis iteration! Instead, an error
        should be generated. Often 'process_placeholder' is a good
        way to either defer or generate an error.

        NOTE: Some methods, such as 'anal_type', 'mark_incomplete' and
              'record_incomplete_ref', call this implicitly, or when needed.
              They are usually preferable to a direct defer() call.
        """
        assert not self.final_iteration, "Must not defer during final iteration"
        if force_progress:
            # Usually, we report progress if we have replaced a placeholder node
            # with an actual valid node. However, sometimes we need to update an
            # existing node *in-place*. For example, this is used by type aliases
            # in context of forward references and/or recursive aliases, and in
            # similar situations (recursive named tuples etc).
            self.progress = True
        self.deferred = True
        # Store debug info for this deferral.
        line = (
            debug_context.line if debug_context else self.statement.line if self.statement else -1
        )
        self.deferral_debug_context.append((self.cur_mod_id, line))

    def track_incomplete_refs(self) -> Tag:
        """Return tag that can be used for tracking references to incomplete names."""
        return self.num_incomplete_refs

    def found_incomplete_ref(self, tag: Tag) -> bool:
        """Have we encountered an incomplete reference since starting tracking?"""
        return self.num_incomplete_refs != tag

    def record_incomplete_ref(self) -> None:
        """Record the encounter of an incomplete reference and defer current analysis target."""
        self.defer()
        self.num_incomplete_refs += 1

    def mark_incomplete(
        self,
        name: str,
        node: Node,
        becomes_typeinfo: bool = False,
        module_public: bool = True,
        module_hidden: bool = False,
    ) -> None:
        """Mark a definition as incomplete (and defer current analysis target).

        Also potentially mark the current namespace as incomplete.

        Args:
            name: The name that we weren't able to define (or '*' if the name is unknown)
            node: The node that refers to the name (definition or lvalue)
            becomes_typeinfo: Pass this to PlaceholderNode (used by special forms like
                named tuples that will create TypeInfos).
        """
        self.defer(node)
        if name == "*":
            self.incomplete = True
        elif not self.is_global_or_nonlocal(name):
            fullname = self.qualified_name(name)
            assert self.statement
            placeholder = PlaceholderNode(
                fullname, node, self.statement.line, becomes_typeinfo=becomes_typeinfo
            )
            self.add_symbol(
                name,
                placeholder,
                module_public=module_public,
                module_hidden=module_hidden,
                context=dummy_context(),
            )
        self.missing_names[-1].add(name)

    def is_incomplete_namespace(self, fullname: str) -> bool:
        """Is a module or class namespace potentially missing some definitions?

        If a name is missing from an incomplete namespace, we'll need to defer the
        current analysis target.
        """
        return fullname in self.incomplete_namespaces

    def process_placeholder(
        self, name: str | None, kind: str, ctx: Context, force_progress: bool = False
    ) -> None:
        """Process a reference targeting placeholder node.

        If this is not a final iteration, defer current node,
        otherwise report an error.

        The 'kind' argument indicates if this a name or attribute expression
        (used for better error message).
        """
        if self.final_iteration:
            self.cannot_resolve_name(name, kind, ctx)
        else:
            self.defer(ctx, force_progress=force_progress)

    def cannot_resolve_name(self, name: str | None, kind: str, ctx: Context) -> None:
        name_format = f' "{name}"' if name else ""
        self.fail(f"Cannot resolve {kind}{name_format} (possible cyclic definition)", ctx)
        if self.is_func_scope():
            self.note("Recursive types are not allowed at function scope", ctx)

    def qualified_name(self, name: str) -> str:
        if self.type is not None:
            return self.type._fullname + "." + name
        elif self.is_func_scope():
            return name
        else:
            return self.cur_mod_id + "." + name

    @contextmanager
    def enter(
        self, function: FuncItem | GeneratorExpr | DictionaryComprehension
    ) -> Iterator[None]:
        """Enter a function, generator or comprehension scope."""
        names = self.saved_locals.setdefault(function, SymbolTable())
        self.locals.append(names)
        is_comprehension = isinstance(function, (GeneratorExpr, DictionaryComprehension))
        self.scope_stack.append(SCOPE_FUNC if not is_comprehension else SCOPE_COMPREHENSION)
        self.global_decls.append(set())
        self.nonlocal_decls.append(set())
        # -1 since entering block will increment this to 0.
        self.block_depth.append(-1)
        self.loop_depth.append(0)
        self.missing_names.append(set())
        try:
            yield
        finally:
            self.locals.pop()
            self.scope_stack.pop()
            self.global_decls.pop()
            self.nonlocal_decls.pop()
            self.block_depth.pop()
            self.loop_depth.pop()
            self.missing_names.pop()

    def is_func_scope(self) -> bool:
        scope_type = self.scope_stack[-1]
        if scope_type == SCOPE_ANNOTATION:
            scope_type = self.scope_stack[-2]
        return scope_type in (SCOPE_FUNC, SCOPE_COMPREHENSION)

    def is_nested_within_func_scope(self) -> bool:
        """Are we underneath a function scope, even if we are in a nested class also?"""
        return any(s in (SCOPE_FUNC, SCOPE_COMPREHENSION) for s in self.scope_stack)

    def is_class_scope(self) -> bool:
        return self.type is not None and not self.is_func_scope()

    def is_module_scope(self) -> bool:
        return not (self.is_class_scope() or self.is_func_scope())

    def current_symbol_kind(self) -> int:
        if self.is_class_scope():
            kind = MDEF
        elif self.is_func_scope():
            kind = LDEF
        else:
            kind = GDEF
        return kind

    def current_symbol_table(
        self, escape_comprehensions: bool = False, type_param: bool = False
    ) -> SymbolTable:
        if type_param and self.scope_stack[-1] == SCOPE_ANNOTATION:
            n = self.locals[-1]
            assert n is not None
            return n
        elif self.is_func_scope():
            if self.scope_stack[-1] == SCOPE_ANNOTATION:
                n = self.locals[-2]
            else:
                n = self.locals[-1]
            assert n is not None
            if escape_comprehensions:
                assert len(self.locals) == len(self.scope_stack)
                # Retrieve the symbol table from the enclosing non-comprehension scope.
                for i, scope_type in enumerate(reversed(self.scope_stack)):
                    if scope_type != SCOPE_COMPREHENSION:
                        if i == len(self.locals) - 1:  # The last iteration.
                            # The caller of the comprehension is in the global space.
                            names = self.globals
                        else:
                            names_candidate = self.locals[-1 - i]
                            assert (
                                names_candidate is not None
                            ), "Escaping comprehension from invalid scope"
                            names = names_candidate
                        break
                else:
                    assert False, "Should have at least one non-comprehension scope"
            else:
                names = n
            assert names is not None
        elif self.type is not None:
            names = self.type.names
        else:
            names = self.globals
        return names

    def is_global_or_nonlocal(self, name: str) -> bool:
        return self.is_func_scope() and (
            name in self.global_decls[-1] or name in self.nonlocal_decls[-1]
        )

    def add_exports(self, exp_or_exps: Iterable[Expression] | Expression) -> None:
        exps = [exp_or_exps] if isinstance(exp_or_exps, Expression) else exp_or_exps
        for exp in exps:
            if isinstance(exp, StrExpr):
                self.all_exports.append(exp.value)

    def name_not_defined(self, name: str, ctx: Context, namespace: str | None = None) -> None:
        incomplete = self.is_incomplete_namespace(namespace or self.cur_mod_id)
        if (
            namespace is None
            and self.type
            and not self.is_func_scope()
            and self.incomplete_type_stack
            and self.incomplete_type_stack[-1]
            and not self.final_iteration
        ):
            # We are processing a class body for the first time, so it is incomplete.
            incomplete = True
        if incomplete:
            # Target namespace is incomplete, so it's possible that the name will be defined
            # later on. Defer current target.
            self.record_incomplete_ref()
            return
        message = f'Name "{name}" is not defined'
        self.fail(message, ctx, code=codes.NAME_DEFINED)

        if f"builtins.{name}" in SUGGESTED_TEST_FIXTURES:
            # The user probably has a missing definition in a test fixture. Let's verify.
            fullname = f"builtins.{name}"
            if self.lookup_fully_qualified_or_none(fullname) is None:
                # Yes. Generate a helpful note.
                self.msg.add_fixture_note(fullname, ctx)

        modules_with_unimported_hints = {
            name.split(".", 1)[0] for name in TYPES_FOR_UNIMPORTED_HINTS
        }
        lowercased = {name.lower(): name for name in TYPES_FOR_UNIMPORTED_HINTS}
        for module in modules_with_unimported_hints:
            fullname = f"{module}.{name}".lower()
            if fullname not in lowercased:
                continue
            # User probably forgot to import these types.
            hint = (
                'Did you forget to import it from "{module}"?'
                ' (Suggestion: "from {module} import {name}")'
            ).format(module=module, name=lowercased[fullname].rsplit(".", 1)[-1])
            self.note(hint, ctx, code=codes.NAME_DEFINED)

    def already_defined(
        self, name: str, ctx: Context, original_ctx: SymbolTableNode | SymbolNode | None, noun: str
    ) -> None:
        if isinstance(original_ctx, SymbolTableNode):
            node: SymbolNode | None = original_ctx.node
        elif isinstance(original_ctx, SymbolNode):
            node = original_ctx
        else:
            node = None

        if isinstance(original_ctx, SymbolTableNode) and isinstance(original_ctx.node, MypyFile):
            # Since this is an import, original_ctx.node points to the module definition.
            # Therefore its line number is always 1, which is not useful for this
            # error message.
            extra_msg = " (by an import)"
        elif node and node.line != -1 and self.is_local_name(node.fullname):
            # TODO: Using previous symbol node may give wrong line. We should use
            #       the line number where the binding was established instead.
            extra_msg = f" on line {node.line}"
        else:
            extra_msg = " (possibly by an import)"
        self.fail(
            f'{noun} "{unmangle(name)}" already defined{extra_msg}', ctx, code=codes.NO_REDEF
        )

    def name_already_defined(
        self, name: str, ctx: Context, original_ctx: SymbolTableNode | SymbolNode | None = None
    ) -> None:
        self.already_defined(name, ctx, original_ctx, noun="Name")

    def attribute_already_defined(
        self, name: str, ctx: Context, original_ctx: SymbolTableNode | SymbolNode | None = None
    ) -> None:
        self.already_defined(name, ctx, original_ctx, noun="Attribute")

    def is_local_name(self, name: str) -> bool:
        """Does name look like reference to a definition in the current module?"""
        return self.is_defined_in_current_module(name) or "." not in name

    def in_checked_function(self) -> bool:
        """Should we type-check the current function?

        - Yes if --check-untyped-defs is set.
        - Yes outside functions.
        - Yes in annotated functions.
        - No otherwise.
        """
        if self.options.check_untyped_defs or not self.function_stack:
            return True

        current_index = len(self.function_stack) - 1
        while current_index >= 0:
            current_func = self.function_stack[current_index]
            if not isinstance(current_func, LambdaExpr):
                return not current_func.is_dynamic()

            # Special case, `lambda` inherits the "checked" state from its parent.
            # Because `lambda` itself cannot be annotated.
            # `lambdas` can be deeply nested, so we try to find at least one other parent.
            current_index -= 1

        # This means that we only have a stack of `lambda` functions,
        # no regular functions.
        return True

    def fail(
        self,
        msg: str | ErrorMessage,
        ctx: Context,
        serious: bool = False,
        *,
        code: ErrorCode | None = None,
        blocker: bool = False,
    ) -> None:
        if not serious and not self.in_checked_function():
            return
        # In case it's a bug and we don't really have context
        assert ctx is not None, msg
        if isinstance(msg, ErrorMessage):
            if code is None:
                code = msg.code
            msg = msg.value
        self.errors.report(
            ctx.line,
            ctx.column,
            msg,
            blocker=blocker,
            code=code,
            end_line=ctx.end_line,
            end_column=ctx.end_column,
        )

    def note(self, msg: str, ctx: Context, code: ErrorCode | None = None) -> None:
        if not self.in_checked_function():
            return
        self.errors.report(ctx.line, ctx.column, msg, severity="note", code=code)

    def incomplete_feature_enabled(self, feature: str, ctx: Context) -> bool:
        if feature not in self.options.enable_incomplete_feature:
            self.fail(
                f'"{feature}" support is experimental,'
                f" use --enable-incomplete-feature={feature} to enable",
                ctx,
            )
            return False
        return True

    def accept(self, node: Node) -> None:
        try:
            node.accept(self)
        except Exception as err:
            report_internal_error(err, self.errors.file, node.line, self.errors, self.options)

    def expr_to_analyzed_type(
        self,
        expr: Expression,
        report_invalid_types: bool = True,
        allow_placeholder: bool = False,
        allow_type_any: bool = False,
        allow_unbound_tvars: bool = False,
        allow_param_spec_literals: bool = False,
        allow_unpack: bool = False,
    ) -> Type | None:
        if isinstance(expr, CallExpr):
            # This is a legacy syntax intended mostly for Python 2, we keep it for
            # backwards compatibility, but new features like generic named tuples
            # and recursive named tuples will be not supported.
            expr.accept(self)
            internal_name, info, tvar_defs = self.named_tuple_analyzer.check_namedtuple(
                expr, None, self.is_func_scope()
            )
            if tvar_defs:
                self.fail("Generic named tuples are not supported for legacy class syntax", expr)
                self.note("Use either Python 3 class syntax, or the assignment syntax", expr)
            if internal_name is None:
                # Some form of namedtuple is the only valid type that looks like a call
                # expression. This isn't a valid type.
                raise TypeTranslationError()
            elif not info:
                self.defer(expr)
                return None
            assert info.tuple_type, "NamedTuple without tuple type"
            fallback = Instance(info, [])
            return TupleType(info.tuple_type.items, fallback=fallback)
        typ = self.expr_to_unanalyzed_type(expr)
        return self.anal_type(
            typ,
            report_invalid_types=report_invalid_types,
            allow_placeholder=allow_placeholder,
            allow_type_any=allow_type_any,
            allow_unbound_tvars=allow_unbound_tvars,
            allow_param_spec_literals=allow_param_spec_literals,
            allow_unpack=allow_unpack,
        )

    def analyze_type_expr(self, expr: Expression) -> None:
        # There are certain expressions that mypy does not need to semantically analyze,
        # since they analyzed solely as type. (For example, indexes in type alias definitions
        # and base classes in class defs). External consumers of the mypy AST may need
        # them semantically analyzed, however, if they need to treat it as an expression
        # and not a type. (Which is to say, mypyc needs to do this.) Do the analysis
        # in a fresh tvar scope in order to suppress any errors about using type variables.
        with self.tvar_scope_frame(TypeVarLikeScope()), self.allow_unbound_tvars_set():
            expr.accept(self)

    def type_analyzer(
        self,
        *,
        tvar_scope: TypeVarLikeScope | None = None,
        allow_tuple_literal: bool = False,
        allow_unbound_tvars: bool = False,
        allow_placeholder: bool = False,
        allow_typed_dict_special_forms: bool = False,
        allow_final: bool = False,
        allow_param_spec_literals: bool = False,
        allow_unpack: bool = False,
        report_invalid_types: bool = True,
        prohibit_self_type: str | None = None,
        prohibit_special_class_field_types: str | None = None,
        allow_type_any: bool = False,
    ) -> TypeAnalyser:
        if tvar_scope is None:
            tvar_scope = self.tvar_scope
        tpan = TypeAnalyser(
            self,
            tvar_scope,
            self.plugin,
            self.options,
            self.cur_mod_node,
            self.is_typeshed_stub_file,
            allow_unbound_tvars=allow_unbound_tvars,
            allow_tuple_literal=allow_tuple_literal,
            report_invalid_types=report_invalid_types,
            allow_placeholder=allow_placeholder,
            allow_typed_dict_special_forms=allow_typed_dict_special_forms,
            allow_final=allow_final,
            allow_param_spec_literals=allow_param_spec_literals,
            allow_unpack=allow_unpack,
            prohibit_self_type=prohibit_self_type,
            prohibit_special_class_field_types=prohibit_special_class_field_types,
            allow_type_any=allow_type_any,
        )
        tpan.in_dynamic_func = bool(self.function_stack and self.function_stack[-1].is_dynamic())
        tpan.global_scope = not self.type and not self.function_stack
        return tpan

    def expr_to_unanalyzed_type(self, node: Expression, allow_unpack: bool = False) -> ProperType:
        return expr_to_unanalyzed_type(
            node, self.options, self.is_stub_file, allow_unpack=allow_unpack
        )

    def anal_type(
        self,
        typ: Type,
        *,
        tvar_scope: TypeVarLikeScope | None = None,
        allow_tuple_literal: bool = False,
        allow_unbound_tvars: bool = False,
        allow_placeholder: bool = False,
        allow_typed_dict_special_forms: bool = False,
        allow_final: bool = False,
        allow_param_spec_literals: bool = False,
        allow_unpack: bool = False,
        report_invalid_types: bool = True,
        prohibit_self_type: str | None = None,
        prohibit_special_class_field_types: str | None = None,
        allow_type_any: bool = False,
    ) -> Type | None:
        """Semantically analyze a type.

        Args:
            typ: Type to analyze (if already analyzed, this is a no-op)
            allow_placeholder: If True, may return PlaceholderType if
                encountering an incomplete definition

        Return None only if some part of the type couldn't be bound *and* it
        referred to an incomplete namespace or definition. In this case also
        defer as needed. During a final iteration this won't return None;
        instead report an error if the type can't be analyzed and return
        AnyType.

        In case of other errors, report an error message and return AnyType.

        NOTE: The caller shouldn't defer even if this returns None or a
              placeholder type.
        """
        has_self_type = find_self_type(
            typ, lambda name: self.lookup_qualified(name, typ, suppress_errors=True)
        )
        if has_self_type and self.type and prohibit_self_type is None:
            self.setup_self_type()
        a = self.type_analyzer(
            tvar_scope=tvar_scope,
            allow_unbound_tvars=allow_unbound_tvars,
            allow_tuple_literal=allow_tuple_literal,
            allow_placeholder=allow_placeholder,
            allow_typed_dict_special_forms=allow_typed_dict_special_forms,
            allow_final=allow_final,
            allow_param_spec_literals=allow_param_spec_literals,
            allow_unpack=allow_unpack,
            report_invalid_types=report_invalid_types,
            prohibit_self_type=prohibit_self_type,
            prohibit_special_class_field_types=prohibit_special_class_field_types,
            allow_type_any=allow_type_any,
        )
        tag = self.track_incomplete_refs()
        typ = typ.accept(a)
        if self.found_incomplete_ref(tag):
            # Something could not be bound yet.
            return None
        self.add_type_alias_deps(a.aliases_used)
        return typ

    def class_type(self, self_type: Type) -> Type:
        return TypeType.make_normalized(self_type)

    def schedule_patch(self, priority: int, patch: Callable[[], None]) -> None:
        self.patches.append((priority, patch))

    def report_hang(self) -> None:
        print("Deferral trace:")
        for mod, line in self.deferral_debug_context:
            print(f"    {mod}:{line}")
        self.errors.report(
            -1,
            -1,
            "INTERNAL ERROR: maximum semantic analysis iteration count reached",
            blocker=True,
        )

    def add_plugin_dependency(self, trigger: str, target: str | None = None) -> None:
        """Add dependency from trigger to a target.

        If the target is not given explicitly, use the current target.
        """
        if target is None:
            target = self.scope.current_target()
        self.cur_mod_node.plugin_deps.setdefault(trigger, set()).add(target)

    def add_type_alias_deps(
        self, aliases_used: Collection[str], target: str | None = None
    ) -> None:
        """Add full names of type aliases on which the current node depends.

        This is used by fine-grained incremental mode to re-check the corresponding nodes.
        If `target` is None, then the target node used will be the current scope.
        """
        if not aliases_used:
            # A basic optimization to avoid adding targets with no dependencies to
            # the `alias_deps` dict.
            return
        if target is None:
            target = self.scope.current_target()
        self.cur_mod_node.alias_deps[target].update(aliases_used)

    def is_mangled_global(self, name: str) -> bool:
        # A global is mangled if there exists at least one renamed variant.
        return unmangle(name) + "'" in self.globals

    def is_initial_mangled_global(self, name: str) -> bool:
        # If there are renamed definitions for a global, the first one has exactly one prime.
        return name == unmangle(name) + "'"

    def parse_bool(self, expr: Expression) -> bool | None:
        # This wrapper is preserved for plugins.
        return parse_bool(expr)

    def parse_str_literal(self, expr: Expression) -> str | None:
        """Attempt to find the string literal value of the given expression. Returns `None` if no
        literal value can be found."""
        if isinstance(expr, StrExpr):
            return expr.value
        if isinstance(expr, RefExpr) and isinstance(expr.node, Var) and expr.node.type is not None:
            values = try_getting_str_literals_from_type(expr.node.type)
            if values is not None and len(values) == 1:
                return values[0]
        return None

    def set_future_import_flags(self, module_name: str) -> None:
        if module_name in FUTURE_IMPORTS:
            self.modules[self.cur_mod_id].future_import_flags.add(FUTURE_IMPORTS[module_name])

    def is_future_flag_set(self, flag: str) -> bool:
        return self.modules[self.cur_mod_id].is_future_flag_set(flag)

    def parse_dataclass_transform_spec(self, call: CallExpr) -> DataclassTransformSpec:
        """Build a DataclassTransformSpec from the arguments passed to the given call to
        typing.dataclass_transform."""
        parameters = DataclassTransformSpec()
        for name, value in zip(call.arg_names, call.args):
            # Skip any positional args. Note that any such args are invalid, but we can rely on
            # typeshed to enforce this and don't need an additional error here.
            if name is None:
                continue

            # field_specifiers is currently the only non-boolean argument; check for it first so
            # so the rest of the block can fail through to handling booleans
            if name == "field_specifiers":
                parameters.field_specifiers = self.parse_dataclass_transform_field_specifiers(
                    value
                )
                continue

            boolean = require_bool_literal_argument(self, value, name)
            if boolean is None:
                continue

            if name == "eq_default":
                parameters.eq_default = boolean
            elif name == "order_default":
                parameters.order_default = boolean
            elif name == "kw_only_default":
                parameters.kw_only_default = boolean
            elif name == "frozen_default":
                parameters.frozen_default = boolean
            else:
                self.fail(f'Unrecognized dataclass_transform parameter "{name}"', call)

        return parameters

    def parse_dataclass_transform_field_specifiers(self, arg: Expression) -> tuple[str, ...]:
        if not isinstance(arg, TupleExpr):
            self.fail('"field_specifiers" argument must be a tuple literal', arg)
            return ()

        names = []
        for specifier in arg.items:
            if not isinstance(specifier, RefExpr):
                self.fail('"field_specifiers" must only contain identifiers', specifier)
                return ()
            names.append(specifier.fullname)
        return tuple(names)

    # leafs
    def visit_int_expr(self, o: IntExpr, /) -> None:
        return None

    def visit_str_expr(self, o: StrExpr, /) -> None:
        return None

    def visit_bytes_expr(self, o: BytesExpr, /) -> None:
        return None

    def visit_float_expr(self, o: FloatExpr, /) -> None:
        return None

    def visit_complex_expr(self, o: ComplexExpr, /) -> None:
        return None

    def visit_ellipsis(self, o: EllipsisExpr, /) -> None:
        return None

    def visit_temp_node(self, o: TempNode, /) -> None:
        return None

    def visit_pass_stmt(self, o: PassStmt, /) -> None:
        return None

    def visit_singleton_pattern(self, o: SingletonPattern, /) -> None:
        return None


def replace_implicit_first_type(sig: FunctionLike, new: Type) -> FunctionLike:
    if isinstance(sig, CallableType):
        if len(sig.arg_types) == 0:
            return sig
        return sig.copy_modified(arg_types=[new] + sig.arg_types[1:])
    elif isinstance(sig, Overloaded):
        return Overloaded(
            [cast(CallableType, replace_implicit_first_type(i, new)) for i in sig.items]
        )
    else:
        assert False


def refers_to_fullname(node: Expression, fullnames: str | tuple[str, ...]) -> bool:
    """Is node a name or member expression with the given full name?"""
    if not isinstance(fullnames, tuple):
        fullnames = (fullnames,)

    if not isinstance(node, RefExpr):
        return False
    if node.fullname in fullnames:
        return True
    if isinstance(node.node, TypeAlias):
        return is_named_instance(node.node.target, fullnames)
    return False


def refers_to_class_or_function(node: Expression) -> bool:
    """Does semantically analyzed node refer to a class?"""
    return isinstance(node, RefExpr) and isinstance(
        node.node, (TypeInfo, FuncDef, OverloadedFuncDef)
    )


def find_duplicate(list: list[T]) -> T | None:
    """If the list has duplicates, return one of the duplicates.

    Otherwise, return None.
    """
    for i in range(1, len(list)):
        if list[i] in list[:i]:
            return list[i]
    return None


def remove_imported_names_from_symtable(names: SymbolTable, module: str) -> None:
    """Remove all imported names from the symbol table of a module."""
    removed: list[str] = []
    for name, node in names.items():
        if node.node is None:
            continue
        fullname = node.node.fullname
        prefix = fullname[: fullname.rfind(".")]
        if prefix != module:
            removed.append(name)
    for name in removed:
        del names[name]


def make_any_non_explicit(t: Type) -> Type:
    """Replace all Any types within in with Any that has attribute 'explicit' set to False"""
    return t.accept(MakeAnyNonExplicit())


class MakeAnyNonExplicit(TrivialSyntheticTypeTranslator):
    def visit_any(self, t: AnyType) -> Type:
        if t.type_of_any == TypeOfAny.explicit:
            return t.copy_modified(TypeOfAny.special_form)
        return t

    def visit_type_alias_type(self, t: TypeAliasType) -> Type:
        return t.copy_modified(args=[a.accept(self) for a in t.args])


def make_any_non_unimported(t: Type) -> Type:
    """Replace all Any types that come from unimported types with special form Any."""
    return t.accept(MakeAnyNonUnimported())


class MakeAnyNonUnimported(TrivialSyntheticTypeTranslator):
    def visit_any(self, t: AnyType) -> Type:
        if t.type_of_any == TypeOfAny.from_unimported_type:
            return t.copy_modified(TypeOfAny.special_form, missing_import_name=None)
        return t

    def visit_type_alias_type(self, t: TypeAliasType) -> Type:
        return t.copy_modified(args=[a.accept(self) for a in t.args])


def apply_semantic_analyzer_patches(patches: list[tuple[int, Callable[[], None]]]) -> None:
    """Call patch callbacks in the right order.

    This should happen after semantic analyzer pass 3.
    """
    patches_by_priority = sorted(patches, key=lambda x: x[0])
    for priority, patch_func in patches_by_priority:
        patch_func()


def names_modified_by_assignment(s: AssignmentStmt) -> list[NameExpr]:
    """Return all unqualified (short) names assigned to in an assignment statement."""
    result: list[NameExpr] = []
    for lvalue in s.lvalues:
        result += names_modified_in_lvalue(lvalue)
    return result


def names_modified_in_lvalue(lvalue: Lvalue) -> list[NameExpr]:
    """Return all NameExpr assignment targets in an Lvalue."""
    if isinstance(lvalue, NameExpr):
        return [lvalue]
    elif isinstance(lvalue, StarExpr):
        return names_modified_in_lvalue(lvalue.expr)
    elif isinstance(lvalue, (ListExpr, TupleExpr)):
        result: list[NameExpr] = []
        for item in lvalue.items:
            result += names_modified_in_lvalue(item)
        return result
    return []


def is_same_var_from_getattr(n1: SymbolNode | None, n2: SymbolNode | None) -> bool:
    """Do n1 and n2 refer to the same Var derived from module-level __getattr__?"""
    return (
        isinstance(n1, Var)
        and n1.from_module_getattr
        and isinstance(n2, Var)
        and n2.from_module_getattr
        and n1.fullname == n2.fullname
    )


def dummy_context() -> Context:
    return TempNode(AnyType(TypeOfAny.special_form))


def is_valid_replacement(old: SymbolTableNode, new: SymbolTableNode) -> bool:
    """Can symbol table node replace an existing one?

    These are the only valid cases:

    1. Placeholder gets replaced with a non-placeholder
    2. Placeholder that isn't known to become type replaced with a
       placeholder that can become a type
    """
    if isinstance(old.node, PlaceholderNode):
        if isinstance(new.node, PlaceholderNode):
            return not old.node.becomes_typeinfo and new.node.becomes_typeinfo
        else:
            return True
    return False


def is_same_symbol(a: SymbolNode | None, b: SymbolNode | None) -> bool:
    return (
        a == b
        or (isinstance(a, PlaceholderNode) and isinstance(b, PlaceholderNode))
        or is_same_var_from_getattr(a, b)
    )


def is_trivial_body(block: Block) -> bool:
    """Returns 'true' if the given body is "trivial" -- if it contains just a "pass",
    "..." (ellipsis), or "raise NotImplementedError()". A trivial body may also
    start with a statement containing just a string (e.g. a docstring).

    Note: Functions that raise other kinds of exceptions do not count as
    "trivial". We use this function to help us determine when it's ok to
    relax certain checks on body, but functions that raise arbitrary exceptions
    are more likely to do non-trivial work. For example:

       def halt(self, reason: str = ...) -> NoReturn:
           raise MyCustomError("Fatal error: " + reason, self.line, self.context)

    A function that raises just NotImplementedError is much less likely to be
    this complex.

    Note: If you update this, you may also need to update
    mypy.fastparse.is_possible_trivial_body!
    """
    body = block.body
    if not body:
        # Functions have empty bodies only if the body is stripped or the function is
        # generated or deserialized. In these cases the body is unknown.
        return False

    # Skip a docstring
    if isinstance(body[0], ExpressionStmt) and isinstance(body[0].expr, StrExpr):
        body = block.body[1:]

    if len(body) == 0:
        # There's only a docstring (or no body at all).
        return True
    elif len(body) > 1:
        return False

    stmt = body[0]

    if isinstance(stmt, RaiseStmt):
        expr = stmt.expr
        if expr is None:
            return False
        if isinstance(expr, CallExpr):
            expr = expr.callee

        return isinstance(expr, NameExpr) and expr.fullname == "builtins.NotImplementedError"

    return isinstance(stmt, PassStmt) or (
        isinstance(stmt, ExpressionStmt) and isinstance(stmt.expr, EllipsisExpr)
    )
