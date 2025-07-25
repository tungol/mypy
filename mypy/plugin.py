"""Plugin system for extending mypy.

At large scale the plugin system works as following:

* Plugins are collected from the corresponding mypy config file option
  (either via paths to Python files, or installed Python modules)
  and imported using importlib.

* Every module should get an entry point function (called 'plugin' by default,
  but may be overridden in the config file) that should accept a single string
  argument that is a full mypy version (includes git commit hash for dev
  versions) and return a subclass of mypy.plugins.Plugin.

* All plugin class constructors should match the signature of mypy.plugin.Plugin
  (i.e. should accept an mypy.options.Options object), and *must* call
  super().__init__().

* At several steps during semantic analysis and type checking mypy calls
  special `get_xxx` methods on user plugins with a single string argument that
  is a fully qualified name (full name) of a relevant definition
  (see mypy.plugin.Plugin method docstrings for details).

* The plugins are called in the order they are passed in the config option.
  Every plugin must decide whether to act on a given full name. The first
  plugin that returns non-None object will be used.

* The above decision should be made using the limited common API specified by
  mypy.plugin.CommonPluginApi.

* The callback returned by the plugin will be called with a larger context that
  includes relevant current state (e.g. a default return type, or a default
  attribute type) and a wider relevant API provider (e.g.
  SemanticAnalyzerPluginInterface or CheckerPluginInterface).

* The result of this is used for further processing. See various `XxxContext`
  named tuples for details about which information is given to each hook.

Plugin developers should ensure that their plugins work well in incremental and
daemon modes. In particular, plugins should not hold global state, and should
always call add_plugin_dependency() in plugin hooks called during semantic
analysis. See the method docstring for more details.

There is no dedicated cache storage for plugins, but plugins can store
per-TypeInfo data in a special .metadata attribute that is serialized to the
mypy caches between incremental runs. To avoid collisions between plugins, they
are encouraged to store their state under a dedicated key coinciding with
plugin name in the metadata dictionary. Every value stored there must be
JSON-serializable.

## Notes about the semantic analyzer

Mypy 0.710 introduced a new semantic analyzer that changed how plugins are
expected to work in several notable ways (from mypy 0.730 the old semantic
analyzer is no longer available):

1. The order of processing AST nodes in modules is different. The old semantic
   analyzer processed modules in textual order, one module at a time. The new
   semantic analyzer first processes the module top levels, including bodies of
   any top-level classes and classes nested within classes. ("Top-level" here
   means "not nested within a function/method".) Functions and methods are
   processed only after module top levels have been finished. If there is an
   import cycle, all module top levels in the cycle are processed before
   processing any functions or methods. Each unit of processing (a module top
   level or a function/method) is called a *target*.

   This also means that function signatures in the same module have not been
   analyzed yet when analyzing the module top level. If you need access to
   a function signature, you'll need to explicitly analyze the signature first
   using `anal_type()`.

2. Each target can be processed multiple times. This may happen if some forward
   references are not ready yet, for example. This means that semantic analyzer
   related plugin hooks can be called multiple times for the same full name.
   These plugin methods must thus be idempotent.

3. The `anal_type` API function returns None if some part of the type is not
   available yet. If this happens, the current target being analyzed will be
   *deferred*, which means that it will be processed again soon, in the hope
   that additional dependencies will be available. This may happen if there are
   forward references to types or inter-module references to types within an
   import cycle.

   Note that if there is a circular definition, mypy may decide to stop
   processing to avoid an infinite number of iterations. When this happens,
   `anal_type` will generate an error and return an `AnyType` type object
   during the final iteration (instead of None).

4. There is a new API method `defer()`. This can be used to explicitly request
   the current target to be reprocessed one more time. You don't need this
   to call this if `anal_type` returns None, however.

5. There is a new API property `final_iteration`, which is true once mypy
   detected no progress during the previous iteration or if the maximum
   semantic analysis iteration count has been reached. You must never
   defer during the final iteration, as it will cause a crash.

6. The `node` attribute of SymbolTableNode objects may contain a reference to
   a PlaceholderNode object. This object means that this definition has not
   been fully processed yet. If you encounter a PlaceholderNode, you should
   defer unless it's the final iteration. If it's the final iteration, you
   should generate an error message. It usually means that there's a cyclic
   definition that cannot be resolved by mypy. PlaceholderNodes can only refer
   to references inside an import cycle. If you are looking up things from
   another module, such as the builtins, that is outside the current module or
   import cycle, you can safely assume that you won't receive a placeholder.

When testing your plugin, you should have a test case that forces a module top
level to be processed multiple times. The easiest way to do this is to include
a forward reference to a class in a top-level annotation. Example:

    c: C  # Forward reference causes second analysis pass
    class C: pass

Note that a forward reference in a function signature won't trigger another
pass, since all functions are processed only after the top level has been fully
analyzed.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, TypeVar

from mypy_extensions import mypyc_attr, trait

from mypy.errorcodes import ErrorCode
from mypy.errors import ErrorInfo
from mypy.lookup import lookup_fully_qualified
from mypy.message_registry import ErrorMessage
from mypy.nodes import (
    ArgKind,
    CallExpr,
    ClassDef,
    Context,
    Expression,
    MypyFile,
    SymbolTableNode,
    TypeInfo,
)
from mypy.options import Options
from mypy.types import (
    CallableType,
    FunctionLike,
    Instance,
    ProperType,
    Type,
    TypeList,
    UnboundType,
)

if TYPE_CHECKING:
    from mypy.messages import MessageBuilder
    from mypy.tvar_scope import TypeVarLikeScope


@trait
class TypeAnalyzerPluginInterface:
    """Interface for accessing semantic analyzer functionality in plugins.

    Methods docstrings contain only basic info. Look for corresponding implementation
    docstrings in typeanal.py for more details.
    """

    # An options object. Note: these are the cloned options for the current file.
    # This might be different from Plugin.options (that contains default/global options)
    # if there are per-file options in the config. This applies to all other interfaces
    # in this file.
    options: Options

    @abstractmethod
    def fail(self, msg: str, ctx: Context, *, code: ErrorCode | None = None) -> None:
        """Emit an error message at given location."""
        raise NotImplementedError

    @abstractmethod
    def named_type(self, fullname: str, args: list[Type], /) -> Instance:
        """Construct an instance of a builtin type with given name."""
        raise NotImplementedError

    @abstractmethod
    def analyze_type(self, typ: Type, /) -> Type:
        """Analyze an unbound type using the default mypy logic."""
        raise NotImplementedError

    @abstractmethod
    def analyze_callable_args(
        self, arglist: TypeList
    ) -> tuple[list[Type], list[ArgKind], list[str | None]] | None:
        """Find types, kinds, and names of arguments from extended callable syntax."""
        raise NotImplementedError


# A context for a hook that semantically analyzes an unbound type.
class AnalyzeTypeContext(NamedTuple):
    type: UnboundType  # Type to analyze
    context: Context  # Relevant location context (e.g. for error messages)
    api: TypeAnalyzerPluginInterface


@mypyc_attr(allow_interpreted_subclasses=True)
class CommonPluginApi:
    """
    A common plugin API (shared between semantic analysis and type checking phases)
    that all plugin hooks get independently of the context.
    """

    # Global mypy options.
    # Per-file options can be only accessed on various
    # XxxPluginInterface classes.
    options: Options

    @abstractmethod
    def lookup_fully_qualified(self, fullname: str) -> SymbolTableNode | None:
        """Lookup a symbol by its full name (including module).

        This lookup function available for all plugins. Return None if a name
        is not found. This function doesn't support lookup from current scope.
        Use SemanticAnalyzerPluginInterface.lookup_qualified() for this."""
        raise NotImplementedError


@trait
class CheckerPluginInterface:
    """Interface for accessing type checker functionality in plugins.

    Methods docstrings contain only basic info. Look for corresponding implementation
    docstrings in checker.py for more details.
    """

    msg: MessageBuilder
    options: Options
    path: str

    # Type context for type inference
    @property
    @abstractmethod
    def type_context(self) -> list[Type | None]:
        """Return the type context of the plugin"""
        raise NotImplementedError

    @abstractmethod
    def fail(
        self, msg: str | ErrorMessage, ctx: Context, /, *, code: ErrorCode | None = None
    ) -> ErrorInfo | None:
        """Emit an error message at given location."""
        raise NotImplementedError

    @abstractmethod
    def named_generic_type(self, name: str, args: list[Type]) -> Instance:
        """Construct an instance of a generic type with given type arguments."""
        raise NotImplementedError

    @abstractmethod
    def get_expression_type(self, node: Expression, type_context: Type | None = None) -> Type:
        """Checks the type of the given expression."""
        raise NotImplementedError


@trait
class SemanticAnalyzerPluginInterface:
    """Interface for accessing semantic analyzer functionality in plugins.

    Methods docstrings contain only basic info. Look for corresponding implementation
    docstrings in semanal.py for more details.

    # TODO: clean-up lookup functions.
    """

    modules: dict[str, MypyFile]
    # Options for current file.
    options: Options
    cur_mod_id: str
    msg: MessageBuilder

    @abstractmethod
    def named_type(self, fullname: str, args: list[Type] | None = None) -> Instance:
        """Construct an instance of a builtin type with given type arguments."""
        raise NotImplementedError

    @abstractmethod
    def builtin_type(self, fully_qualified_name: str) -> Instance:
        """Legacy function -- use named_type() instead."""
        # NOTE: Do not delete this since many plugins may still use it.
        raise NotImplementedError

    @abstractmethod
    def named_type_or_none(self, fullname: str, args: list[Type] | None = None) -> Instance | None:
        """Construct an instance of a type with given type arguments.

        Return None if a type could not be constructed for the qualified
        type name. This is possible when the qualified name includes a
        module name and the module has not been imported.
        """
        raise NotImplementedError

    @abstractmethod
    def basic_new_typeinfo(self, name: str, basetype_or_fallback: Instance, line: int) -> TypeInfo:
        raise NotImplementedError

    @abstractmethod
    def parse_bool(self, expr: Expression) -> bool | None:
        """Parse True/False literals."""
        raise NotImplementedError

    @abstractmethod
    def parse_str_literal(self, expr: Expression) -> str | None:
        """Parse string literals."""

    @abstractmethod
    def fail(
        self,
        msg: str,
        ctx: Context,
        serious: bool = False,
        *,
        blocker: bool = False,
        code: ErrorCode | None = None,
    ) -> None:
        """Emit an error message at given location."""
        raise NotImplementedError

    @abstractmethod
    def anal_type(
        self,
        typ: Type,
        /,
        *,
        tvar_scope: TypeVarLikeScope | None = None,
        allow_tuple_literal: bool = False,
        allow_unbound_tvars: bool = False,
        report_invalid_types: bool = True,
    ) -> Type | None:
        """Analyze an unbound type.

        Return None if some part of the type is not ready yet. In this
        case the current target being analyzed will be deferred and
        analyzed again.
        """
        raise NotImplementedError

    @abstractmethod
    def class_type(self, self_type: Type) -> Type:
        """Generate type of first argument of class methods from type of self."""
        raise NotImplementedError

    @abstractmethod
    def lookup_fully_qualified(self, fullname: str, /) -> SymbolTableNode:
        """Lookup a symbol by its fully qualified name.

        Raise an error if not found.
        """
        raise NotImplementedError

    @abstractmethod
    def lookup_fully_qualified_or_none(self, fullname: str, /) -> SymbolTableNode | None:
        """Lookup a symbol by its fully qualified name.

        Return None if not found.
        """
        raise NotImplementedError

    @abstractmethod
    def lookup_qualified(
        self, name: str, ctx: Context, suppress_errors: bool = False
    ) -> SymbolTableNode | None:
        """Lookup symbol using a name in current scope.

        This follows Python local->non-local->global->builtins rules.
        """
        raise NotImplementedError

    @abstractmethod
    def add_plugin_dependency(self, trigger: str, target: str | None = None) -> None:
        """Specify semantic dependencies for generated methods/variables.

        If the symbol with full name given by trigger is found to be stale by mypy,
        then the body of node with full name given by target will be re-checked.
        By default, this is the node that is currently analyzed.

        For example, the dataclass plugin adds a generated __init__ method with
        a signature that depends on types of attributes in ancestor classes. If any
        attribute in an ancestor class gets stale (modified), we need to reprocess
        the subclasses (and thus regenerate __init__ methods).

        This is used by fine-grained incremental mode (mypy daemon). See mypy/server/deps.py
        for more details.
        """
        raise NotImplementedError

    @abstractmethod
    def add_symbol_table_node(self, name: str, symbol: SymbolTableNode) -> Any:
        """Add node to global symbol table (or to nearest class if there is one)."""
        raise NotImplementedError

    @abstractmethod
    def qualified_name(self, name: str) -> str:
        """Make qualified name using current module and enclosing class (if any)."""
        raise NotImplementedError

    @abstractmethod
    def defer(self) -> None:
        """Call this to defer the processing of the current node.

        This will request an additional iteration of semantic analysis.
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def final_iteration(self) -> bool:
        """Is this the final iteration of semantic analysis?"""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_stub_file(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def analyze_simple_literal_type(self, rvalue: Expression, is_final: bool) -> Type | None:
        raise NotImplementedError


# A context for querying for configuration data about a module for
# cache invalidation purposes.
class ReportConfigContext(NamedTuple):
    id: str  # Module name
    path: str  # Module file path
    is_check: bool  # Is this invocation for checking whether the config matches


# A context for a function signature hook that infers a better signature for a
# function.  Note that argument types aren't available yet.  If you need them,
# you have to use a method hook instead.
class FunctionSigContext(NamedTuple):
    args: list[list[Expression]]  # Actual expressions for each formal argument
    default_signature: CallableType  # Original signature of the method
    context: Context  # Relevant location context (e.g. for error messages)
    api: CheckerPluginInterface


# A context for a function hook that infers the return type of a function with
# a special signature.
#
# A no-op callback would just return the inferred return type, but a useful
# callback at least sometimes can infer a more precise type.
class FunctionContext(NamedTuple):
    arg_types: list[list[Type]]  # List of actual caller types for each formal argument
    arg_kinds: list[list[ArgKind]]  # Ditto for argument kinds, see nodes.ARG_* constants
    # Names of formal parameters from the callee definition,
    # these will be sufficient in most cases.
    callee_arg_names: list[str | None]
    # Names of actual arguments in the call expression. For example,
    # in a situation like this:
    #     def func(**kwargs) -> None:
    #         pass
    #     func(kw1=1, kw2=2)
    # callee_arg_names will be ['kwargs'] and arg_names will be [['kw1', 'kw2']].
    arg_names: list[list[str | None]]
    default_return_type: Type  # Return type inferred from signature
    args: list[list[Expression]]  # Actual expressions for each formal argument
    context: Context  # Relevant location context (e.g. for error messages)
    api: CheckerPluginInterface


# A context for a method signature hook that infers a better signature for a
# method.  Note that argument types aren't available yet.  If you need them,
# you have to use a method hook instead.
# TODO: document ProperType in the plugin changelog/update issue.
class MethodSigContext(NamedTuple):
    type: ProperType  # Base object type for method call
    args: list[list[Expression]]  # Actual expressions for each formal argument
    default_signature: CallableType  # Original signature of the method
    context: Context  # Relevant location context (e.g. for error messages)
    api: CheckerPluginInterface


# A context for a method hook that infers the return type of a method with a
# special signature.
#
# This is very similar to FunctionContext (only differences are documented).
class MethodContext(NamedTuple):
    type: ProperType  # Base object type for method call
    arg_types: list[list[Type]]  # List of actual caller types for each formal argument
    # see FunctionContext for details about names and kinds
    arg_kinds: list[list[ArgKind]]
    callee_arg_names: list[str | None]
    arg_names: list[list[str | None]]
    default_return_type: Type  # Return type inferred by mypy
    args: list[list[Expression]]  # Lists of actual expressions for every formal argument
    context: Context
    api: CheckerPluginInterface


# A context for an attribute type hook that infers the type of an attribute.
class AttributeContext(NamedTuple):
    type: ProperType  # Type of object with attribute
    default_attr_type: Type  # Original attribute type
    is_lvalue: bool  # Whether the attribute is the target of an assignment
    context: Context  # Relevant location context (e.g. for error messages)
    api: CheckerPluginInterface


# A context for a class hook that modifies the class definition.
class ClassDefContext(NamedTuple):
    cls: ClassDef  # The class definition
    reason: Expression  # The expression being applied (decorator, metaclass, base class)
    api: SemanticAnalyzerPluginInterface


# A context for dynamic class definitions like
# Base = declarative_base()
class DynamicClassDefContext(NamedTuple):
    call: CallExpr  # The r.h.s. of dynamic class definition
    name: str  # The name this class is being assigned to
    api: SemanticAnalyzerPluginInterface


@mypyc_attr(allow_interpreted_subclasses=True)
class Plugin(CommonPluginApi):
    """Base class of all type checker plugins.

    This defines a no-op plugin.  Subclasses can override some methods to
    provide some actual functionality.

    All get_ methods are treated as pure functions (you should assume that
    results might be cached). A plugin should return None from a get_ method
    to give way to other plugins.

    Look at the comments of various *Context objects for additional information on
    various hooks.
    """

    def __init__(self, options: Options) -> None:
        self.options = options
        self.python_version = options.python_version
        # This can't be set in __init__ because it is executed too soon in build.py.
        # Therefore, build.py *must* set it later before graph processing starts
        # by calling set_modules().
        self._modules: dict[str, MypyFile] | None = None

    def set_modules(self, modules: dict[str, MypyFile]) -> None:
        self._modules = modules

    def lookup_fully_qualified(self, fullname: str) -> SymbolTableNode | None:
        assert self._modules is not None
        return lookup_fully_qualified(fullname, self._modules)

    def report_config_data(self, ctx: ReportConfigContext) -> Any:
        """Get representation of configuration data for a module.

        The data must be encodable as JSON and will be stored in the
        cache metadata for the module. A mismatch between the cached
        values and the returned will result in that module's cache
        being invalidated and the module being rechecked.

        This can be called twice for each module, once after loading
        the cache to check if it is valid and once while writing new
        cache information.

        If is_check in the context is true, then the return of this
        call will be checked against the cached version. Otherwise the
        call is being made to determine what to put in the cache. This
        can be used to allow consulting extra cache files in certain
        complex situations.

        This can be used to incorporate external configuration information
        that might require changes to typechecking.
        """
        return None

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        """Customize dependencies for a module.

        This hook allows adding in new dependencies for a module. It
        is called after parsing a file but before analysis. This can
        be useful if a library has dependencies that are dynamic based
        on configuration information, for example.

        Returns a list of (priority, module name, line number) tuples.

        The line number can be -1 when there is not a known real line number.

        Priorities are defined in mypy.build (but maybe shouldn't be).
        10 is a good choice for priority.
        """
        return []

    def get_type_analyze_hook(self, fullname: str) -> Callable[[AnalyzeTypeContext], Type] | None:
        """Customize behaviour of the type analyzer for given full names.

        This method is called during the semantic analysis pass whenever mypy sees an
        unbound type. For example, while analysing this code:

            from lib import Special, Other

            var: Special
            def func(x: Other[int]) -> None:
                ...

        this method will be called with 'lib.Special', and then with 'lib.Other'.
        The callback returned by plugin must return an analyzed type,
        i.e. an instance of `mypy.types.Type`.
        """
        return None

    def get_function_signature_hook(
        self, fullname: str
    ) -> Callable[[FunctionSigContext], FunctionLike] | None:
        """Adjust the signature of a function.

        This method is called before type checking a function call. Plugin
        may infer a better type for the function.

            from lib import Class, do_stuff

            do_stuff(42)
            Class()

        This method will be called with 'lib.do_stuff' and then with 'lib.Class'.
        """
        return None

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        """Adjust the return type of a function call.

        This method is called after type checking a call. Plugin may adjust the return
        type inferred by mypy, and/or emit some error messages. Note, this hook is also
        called for class instantiation calls, so that in this example:

            from lib import Class, do_stuff

            do_stuff(42)
            Class()

        This method will be called with 'lib.do_stuff' and then with 'lib.Class'.
        """
        return None

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        """Adjust the signature of a method.

        This method is called before type checking a method call. Plugin
        may infer a better type for the method. The hook is also called for special
        Python dunder methods except __init__ and __new__ (use get_function_hook to customize
        class instantiation). This function is called with the method full name using
        the class where it was _defined_. For example, in this code:

            from lib import Special

            class Base:
                def method(self, arg: Any) -> Any:
                    ...
            class Derived(Base):
                ...

            var: Derived
            var.method(42)

            x: Special
            y = x[0]

        this method is called with '__main__.Base.method', and then with
        'lib.Special.__getitem__'.
        """
        return None

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Adjust return type of a method call.

        This is the same as get_function_hook(), but is called with the
        method full name (again, using the class where the method is defined).
        """
        return None

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Adjust type of an instance attribute.

        This method is called with attribute full name using the class of the instance where
        the attribute was defined (or Var.info.fullname for generated attributes).

        For classes without __getattr__ or __getattribute__, this hook is only called for
        names of fields/properties (but not methods) that exist in the instance MRO.

        For classes that implement __getattr__ or __getattribute__, this hook is called
        for all fields/properties, including nonexistent ones (but still not methods).

        For example:

            class Base:
                x: Any
                def __getattr__(self, attr: str) -> Any: ...

            class Derived(Base):
                ...

            var: Derived
            var.x
            var.y

        get_attribute_hook is called with '__main__.Base.x' and '__main__.Base.y'.
        However, if we had not implemented __getattr__ on Base, you would only get
        the callback for 'var.x'; 'var.y' would produce an error without calling the hook.
        """
        return None

    def get_class_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """
        Adjust type of a class attribute.

        This method is called with attribute full name using the class where the attribute was
        defined (or Var.info.fullname for generated attributes).

        For example:

            class Cls:
                x: Any

            Cls.x

        get_class_attribute_hook is called with '__main__.Cls.x' as fullname.
        """
        return None

    def get_class_decorator_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Update class definition for given class decorators.

        The plugin can modify a TypeInfo _in place_ (for example add some generated
        methods to the symbol table). This hook is called after the class body was
        semantically analyzed, but *there may still be placeholders* (typically
        caused by forward references).

        NOTE: Usually get_class_decorator_hook_2 is the better option, since it
              guarantees that there are no placeholders.

        The hook is called with full names of all class decorators.

        The hook can be called multiple times per class, so it must be
        idempotent.
        """
        return None

    def get_class_decorator_hook_2(
        self, fullname: str
    ) -> Callable[[ClassDefContext], bool] | None:
        """Update class definition for given class decorators.

        Similar to get_class_decorator_hook, but this runs in a later pass when
        placeholders have been resolved.

        The hook can return False if some base class hasn't been
        processed yet using class hooks. It causes all class hooks
        (that are run in this same pass) to be invoked another time for
        the file(s) currently being processed.

        The hook can be called multiple times per class, so it must be
        idempotent.
        """
        return None

    def get_metaclass_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Update class definition for given declared metaclasses.

        Same as get_class_decorator_hook() but for metaclasses. Note:
        this hook will be only called for explicit metaclasses, not for
        inherited ones.

        TODO: probably it should also be called on inherited metaclasses.
        """
        return None

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Update class definition for given base classes.

        Same as get_class_decorator_hook() but for base classes. Base classes
        don't need to refer to TypeInfos, if a base class refers to a variable with
        Any type, this hook will still be called.
        """
        return None

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Customize MRO for given classes.

        The plugin can modify the class MRO _in place_. This method is called
        with the class full name before its body was semantically analyzed.
        """
        return None

    def get_dynamic_class_hook(
        self, fullname: str
    ) -> Callable[[DynamicClassDefContext], None] | None:
        """Semantically analyze a dynamic class definition.

        This plugin hook allows one to semantically analyze dynamic class definitions like:

            from lib import dynamic_class

            X = dynamic_class('X', [])

        For such definition, this hook will be called with 'lib.dynamic_class'.
        The plugin should create the corresponding TypeInfo, and place it into a relevant
        symbol table, e.g. using ctx.api.add_symbol_table_node().
        """
        return None


T = TypeVar("T")


class ChainedPlugin(Plugin):
    """A plugin that represents a sequence of chained plugins.

    Each lookup method returns the hook for the first plugin that
    reports a match.

    This class should not be subclassed -- use Plugin as the base class
    for all plugins.
    """

    # TODO: Support caching of lookup results (through a LRU cache, for example).

    def __init__(self, options: Options, plugins: list[Plugin]) -> None:
        """Initialize chained plugin.

        Assume that the child plugins aren't mutated (results may be cached).
        """
        super().__init__(options)
        self._plugins = plugins

    def set_modules(self, modules: dict[str, MypyFile]) -> None:
        for plugin in self._plugins:
            plugin.set_modules(modules)

    def report_config_data(self, ctx: ReportConfigContext) -> Any:
        config_data = [plugin.report_config_data(ctx) for plugin in self._plugins]
        return config_data if any(x is not None for x in config_data) else None

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        deps = []
        for plugin in self._plugins:
            deps.extend(plugin.get_additional_deps(file))
        return deps

    def get_type_analyze_hook(self, fullname: str) -> Callable[[AnalyzeTypeContext], Type] | None:
        return self._find_hook(lambda plugin: plugin.get_type_analyze_hook(fullname))

    def get_function_signature_hook(
        self, fullname: str
    ) -> Callable[[FunctionSigContext], FunctionLike] | None:
        return self._find_hook(lambda plugin: plugin.get_function_signature_hook(fullname))

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        return self._find_hook(lambda plugin: plugin.get_function_hook(fullname))

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        return self._find_hook(lambda plugin: plugin.get_method_signature_hook(fullname))

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        return self._find_hook(lambda plugin: plugin.get_method_hook(fullname))

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        return self._find_hook(lambda plugin: plugin.get_attribute_hook(fullname))

    def get_class_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        return self._find_hook(lambda plugin: plugin.get_class_attribute_hook(fullname))

    def get_class_decorator_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        return self._find_hook(lambda plugin: plugin.get_class_decorator_hook(fullname))

    def get_class_decorator_hook_2(
        self, fullname: str
    ) -> Callable[[ClassDefContext], bool] | None:
        return self._find_hook(lambda plugin: plugin.get_class_decorator_hook_2(fullname))

    def get_metaclass_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        return self._find_hook(lambda plugin: plugin.get_metaclass_hook(fullname))

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        return self._find_hook(lambda plugin: plugin.get_base_class_hook(fullname))

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        return self._find_hook(lambda plugin: plugin.get_customize_class_mro_hook(fullname))

    def get_dynamic_class_hook(
        self, fullname: str
    ) -> Callable[[DynamicClassDefContext], None] | None:
        return self._find_hook(lambda plugin: plugin.get_dynamic_class_hook(fullname))

    def _find_hook(self, lookup: Callable[[Plugin], T]) -> T | None:
        for plugin in self._plugins:
            hook = lookup(plugin)
            if hook:
                return hook
        return None
