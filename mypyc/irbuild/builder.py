"""Builder class to transform a mypy AST to the IR form.

See the docstring of class IRBuilder for more information.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Callable, Final, Union, overload

from mypy.build import Graph
from mypy.maptype import map_instance_to_supertype
from mypy.nodes import (
    ARG_NAMED,
    ARG_POS,
    GDEF,
    LDEF,
    PARAM_SPEC_KIND,
    TYPE_VAR_KIND,
    TYPE_VAR_TUPLE_KIND,
    ArgKind,
    CallExpr,
    Decorator,
    Expression,
    FuncDef,
    IndexExpr,
    IntExpr,
    Lvalue,
    MemberExpr,
    MypyFile,
    NameExpr,
    OpExpr,
    OverloadedFuncDef,
    RefExpr,
    StarExpr,
    Statement,
    SymbolNode,
    TupleExpr,
    TypeAlias,
    TypeInfo,
    TypeParam,
    UnaryExpr,
    Var,
)
from mypy.types import (
    AnyType,
    DeletedType,
    Instance,
    ProperType,
    TupleType,
    Type,
    TypedDictType,
    TypeOfAny,
    TypeVarLikeType,
    UninhabitedType,
    UnionType,
    get_proper_type,
)
from mypy.util import module_prefix, split_target
from mypy.visitor import ExpressionVisitor, StatementVisitor
from mypyc.common import BITMAP_BITS, SELF_NAME, TEMP_ATTR_NAME
from mypyc.crash import catch_errors
from mypyc.errors import Errors
from mypyc.ir.class_ir import ClassIR, NonExtClassInfo
from mypyc.ir.func_ir import INVALID_FUNC_DEF, FuncDecl, FuncIR, FuncSignature, RuntimeArg
from mypyc.ir.ops import (
    NAMESPACE_MODULE,
    NAMESPACE_TYPE_VAR,
    Assign,
    BasicBlock,
    Branch,
    ComparisonOp,
    GetAttr,
    InitStatic,
    Integer,
    IntOp,
    LoadStatic,
    Op,
    PrimitiveDescription,
    RaiseStandardError,
    Register,
    SetAttr,
    TupleGet,
    Unreachable,
    Value,
)
from mypyc.ir.rtypes import (
    RInstance,
    RTuple,
    RType,
    RUnion,
    bitmap_rprimitive,
    c_pyssize_t_rprimitive,
    dict_rprimitive,
    int_rprimitive,
    is_float_rprimitive,
    is_list_rprimitive,
    is_none_rprimitive,
    is_object_rprimitive,
    is_tagged,
    is_tuple_rprimitive,
    none_rprimitive,
    object_rprimitive,
    str_rprimitive,
)
from mypyc.irbuild.context import FuncInfo, ImplicitClass
from mypyc.irbuild.ll_builder import LowLevelIRBuilder
from mypyc.irbuild.mapper import Mapper
from mypyc.irbuild.nonlocalcontrol import (
    BaseNonlocalControl,
    GeneratorNonlocalControl,
    LoopNonlocalControl,
    NonlocalControl,
)
from mypyc.irbuild.prebuildvisitor import PreBuildVisitor
from mypyc.irbuild.prepare import RegisterImplInfo
from mypyc.irbuild.targets import (
    AssignmentTarget,
    AssignmentTargetAttr,
    AssignmentTargetIndex,
    AssignmentTargetRegister,
    AssignmentTargetTuple,
)
from mypyc.irbuild.util import bytes_from_str, is_constant
from mypyc.options import CompilerOptions
from mypyc.primitives.dict_ops import dict_get_item_op, dict_set_item_op
from mypyc.primitives.generic_ops import iter_op, next_op, py_setattr_op
from mypyc.primitives.list_ops import list_get_item_unsafe_op, list_pop_last, to_list
from mypyc.primitives.misc_ops import check_unpack_count_op, get_module_dict_op, import_op
from mypyc.primitives.registry import CFunctionDescription, function_ops

# These int binary operations can borrow their operands safely, since the
# primitives take this into consideration.
int_borrow_friendly_op: Final = {"+", "-", "==", "!=", "<", "<=", ">", ">="}


class IRVisitor(ExpressionVisitor[Value], StatementVisitor[None]):
    pass


class UnsupportedException(Exception):
    pass


SymbolTarget = Union[AssignmentTargetRegister, AssignmentTargetAttr]


class IRBuilder:
    """Builder class used to construct mypyc IR from a mypy AST.

    The IRBuilder class maintains IR transformation state and provides access
    to various helpers used to implement the transform.

    mypyc.irbuild.visitor.IRBuilderVisitor is used to dispatch based on mypy
    AST node type to code that actually does the bulk of the work. For
    example, expressions are transformed in mypyc.irbuild.expression and
    functions are transformed in mypyc.irbuild.function.

    Use the "accept()" method to translate individual mypy AST nodes to IR.
    Other methods are used to generate IR for various lower-level operations.

    This class wraps the lower-level LowLevelIRBuilder class, an instance
    of which is available through the "builder" attribute. The low-level
    builder class doesn't have any knowledge of the mypy AST. Wrappers for
    some LowLevelIRBuilder method are provided for convenience, but others
    can also be accessed via the "builder" attribute.

    See also:
     * The mypyc IR is defined in the mypyc.ir package.
     * The top-level IR transform control logic is in mypyc.irbuild.main.
    """

    def __init__(
        self,
        current_module: str,
        types: dict[Expression, Type],
        graph: Graph,
        errors: Errors,
        mapper: Mapper,
        pbv: PreBuildVisitor,
        visitor: IRVisitor,
        options: CompilerOptions,
        singledispatch_impls: dict[FuncDef, list[RegisterImplInfo]],
    ) -> None:
        self.builder = LowLevelIRBuilder(errors, options)
        self.builders = [self.builder]
        self.symtables: list[dict[SymbolNode, SymbolTarget]] = [{}]
        self.runtime_args: list[list[RuntimeArg]] = [[]]
        self.function_name_stack: list[str] = []
        self.class_ir_stack: list[ClassIR] = []
        # Keep track of whether the next statement in a block is reachable
        # or not, separately for each block nesting level
        self.block_reachable_stack: list[bool] = [True]

        self.current_module = current_module
        self.mapper = mapper
        self.types = types
        self.graph = graph
        self.ret_types: list[RType] = []
        self.functions: list[FuncIR] = []
        self.function_names: set[tuple[str | None, str]] = set()
        self.classes: list[ClassIR] = []
        self.final_names: list[tuple[str, RType]] = []
        self.type_var_names: list[str] = []
        self.callable_class_names: set[str] = set()
        self.options = options

        # These variables keep track of the number of lambdas, implicit indices, and implicit
        # iterators instantiated so we avoid name conflicts. The indices and iterators are
        # instantiated from for-loops.
        self.lambda_counter = 0
        self.temp_counter = 0

        # These variables are populated from the first-pass PreBuildVisitor.
        self.free_variables = pbv.free_variables
        self.prop_setters = pbv.prop_setters
        self.encapsulating_funcs = pbv.encapsulating_funcs
        self.nested_fitems = pbv.nested_funcs.keys()
        self.fdefs_to_decorators = pbv.funcs_to_decorators
        self.module_import_groups = pbv.module_import_groups

        self.singledispatch_impls = singledispatch_impls

        self.visitor = visitor

        # This list operates similarly to a function call stack for nested functions. Whenever a
        # function definition begins to be generated, a FuncInfo instance is added to the stack,
        # and information about that function (e.g. whether it is nested, its environment class to
        # be generated) is stored in that FuncInfo instance. When the function is done being
        # generated, its corresponding FuncInfo is popped off the stack.
        self.fn_info = FuncInfo(INVALID_FUNC_DEF, "", "")
        self.fn_infos: list[FuncInfo] = [self.fn_info]

        # This list operates as a stack of constructs that modify the
        # behavior of nonlocal control flow constructs.
        self.nonlocal_control: list[NonlocalControl] = []

        self.errors = errors
        # Notionally a list of all of the modules imported by the
        # module being compiled, but stored as an OrderedDict so we
        # can also do quick lookups.
        self.imports: dict[str, None] = {}

        self.can_borrow = False

    # High-level control

    def set_module(self, module_name: str, module_path: str) -> None:
        """Set the name and path of the current module.

        This must be called before transforming any AST nodes.
        """
        self.module_name = module_name
        self.module_path = module_path
        self.builder.set_module(module_name, module_path)

    @overload
    def accept(self, node: Expression, *, can_borrow: bool = False) -> Value: ...

    @overload
    def accept(self, node: Statement) -> None: ...

    def accept(self, node: Statement | Expression, *, can_borrow: bool = False) -> Value | None:
        """Transform an expression or a statement.

        If can_borrow is true, prefer to generate a borrowed reference.
        Borrowed references are faster since they don't require reference count
        manipulation, but they are only safe to use in specific contexts.
        """
        with self.catch_errors(node.line):
            if isinstance(node, Expression):
                old_can_borrow = self.can_borrow
                self.can_borrow = can_borrow
                try:
                    res = node.accept(self.visitor)
                    res = self.coerce(res, self.node_type(node), node.line)
                # If we hit an error during compilation, we want to
                # keep trying, so we can produce more error
                # messages. Generate a temp of the right type to keep
                # from causing more downstream trouble.
                except UnsupportedException:
                    res = Register(self.node_type(node))
                self.can_borrow = old_can_borrow
                if not can_borrow:
                    self.flush_keep_alives()
                return res
            else:
                try:
                    node.accept(self.visitor)
                except UnsupportedException:
                    pass
                return None

    def flush_keep_alives(self) -> None:
        self.builder.flush_keep_alives()

    # Pass through methods for the most common low-level builder ops, for convenience.

    def add(self, op: Op) -> Value:
        return self.builder.add(op)

    def goto(self, target: BasicBlock) -> None:
        self.builder.goto(target)

    def activate_block(self, block: BasicBlock) -> None:
        self.builder.activate_block(block)

    def goto_and_activate(self, block: BasicBlock) -> None:
        self.builder.goto_and_activate(block)

    def self(self) -> Register:
        return self.builder.self()

    def py_get_attr(self, obj: Value, attr: str, line: int) -> Value:
        return self.builder.py_get_attr(obj, attr, line)

    def load_str(self, value: str) -> Value:
        return self.builder.load_str(value)

    def load_bytes_from_str_literal(self, value: str) -> Value:
        """Load bytes object from a string literal.

        The literal characters of BytesExpr (the characters inside b'')
        are stored in BytesExpr.value, whose type is 'str' not 'bytes'.
        Thus we perform a special conversion here.
        """
        return self.builder.load_bytes(bytes_from_str(value))

    def load_int(self, value: int) -> Value:
        return self.builder.load_int(value)

    def load_float(self, value: float) -> Value:
        return self.builder.load_float(value)

    def unary_op(self, lreg: Value, expr_op: str, line: int) -> Value:
        return self.builder.unary_op(lreg, expr_op, line)

    def binary_op(self, lreg: Value, rreg: Value, expr_op: str, line: int) -> Value:
        return self.builder.binary_op(lreg, rreg, expr_op, line)

    def coerce(self, src: Value, target_type: RType, line: int, force: bool = False) -> Value:
        return self.builder.coerce(src, target_type, line, force, can_borrow=self.can_borrow)

    def none_object(self) -> Value:
        return self.builder.none_object()

    def none(self) -> Value:
        return self.builder.none()

    def true(self) -> Value:
        return self.builder.true()

    def false(self) -> Value:
        return self.builder.false()

    def new_list_op(self, values: list[Value], line: int) -> Value:
        return self.builder.new_list_op(values, line)

    def new_set_op(self, values: list[Value], line: int) -> Value:
        return self.builder.new_set_op(values, line)

    def translate_is_op(self, lreg: Value, rreg: Value, expr_op: str, line: int) -> Value:
        return self.builder.translate_is_op(lreg, rreg, expr_op, line)

    def py_call(
        self,
        function: Value,
        arg_values: list[Value],
        line: int,
        arg_kinds: list[ArgKind] | None = None,
        arg_names: Sequence[str | None] | None = None,
    ) -> Value:
        return self.builder.py_call(function, arg_values, line, arg_kinds, arg_names)

    def add_bool_branch(self, value: Value, true: BasicBlock, false: BasicBlock) -> None:
        self.builder.add_bool_branch(value, true, false)

    def load_native_type_object(self, fullname: str) -> Value:
        return self.builder.load_native_type_object(fullname)

    def gen_method_call(
        self,
        base: Value,
        name: str,
        arg_values: list[Value],
        result_type: RType | None,
        line: int,
        arg_kinds: list[ArgKind] | None = None,
        arg_names: list[str | None] | None = None,
    ) -> Value:
        return self.builder.gen_method_call(
            base, name, arg_values, result_type, line, arg_kinds, arg_names, self.can_borrow
        )

    def load_module(self, name: str) -> Value:
        return self.builder.load_module(name)

    def call_c(self, desc: CFunctionDescription, args: list[Value], line: int) -> Value:
        return self.builder.call_c(desc, args, line)

    def primitive_op(
        self,
        desc: PrimitiveDescription,
        args: list[Value],
        line: int,
        result_type: RType | None = None,
    ) -> Value:
        return self.builder.primitive_op(desc, args, line, result_type)

    def int_op(self, type: RType, lhs: Value, rhs: Value, op: int, line: int) -> Value:
        return self.builder.int_op(type, lhs, rhs, op, line)

    def compare_tuples(self, lhs: Value, rhs: Value, op: str, line: int) -> Value:
        return self.builder.compare_tuples(lhs, rhs, op, line)

    def builtin_len(self, val: Value, line: int) -> Value:
        return self.builder.builtin_len(val, line)

    def new_tuple(self, items: list[Value], line: int) -> Value:
        return self.builder.new_tuple(items, line)

    def debug_print(self, toprint: str | Value) -> None:
        return self.builder.debug_print(toprint)

    # Helpers for IR building

    def add_to_non_ext_dict(
        self, non_ext: NonExtClassInfo, key: str, val: Value, line: int
    ) -> None:
        # Add an attribute entry into the class dict of a non-extension class.
        key_unicode = self.load_str(key)
        self.primitive_op(dict_set_item_op, [non_ext.dict, key_unicode, val], line)

    def gen_import(self, id: str, line: int) -> None:
        self.imports[id] = None

        needs_import, out = BasicBlock(), BasicBlock()
        self.check_if_module_loaded(id, line, needs_import, out)

        self.activate_block(needs_import)
        value = self.call_c(import_op, [self.load_str(id)], line)
        self.add(InitStatic(value, id, namespace=NAMESPACE_MODULE))
        self.goto_and_activate(out)

    def check_if_module_loaded(
        self, id: str, line: int, needs_import: BasicBlock, out: BasicBlock
    ) -> None:
        """Generate code that checks if the module `id` has been loaded yet.

        Arguments:
            id: name of module to check if imported
            line: line number that the import occurs on
            needs_import: the BasicBlock that is run if the module has not been loaded yet
            out: the BasicBlock that is run if the module has already been loaded"""
        first_load = self.load_module(id)
        comparison = self.translate_is_op(first_load, self.none_object(), "is not", line)
        self.add_bool_branch(comparison, out, needs_import)

    def get_module(self, module: str, line: int) -> Value:
        # Python 3.7 has a nice 'PyImport_GetModule' function that we can't use :(
        mod_dict = self.call_c(get_module_dict_op, [], line)
        # Get module object from modules dict.
        return self.primitive_op(dict_get_item_op, [mod_dict, self.load_str(module)], line)

    def get_module_attr(self, module: str, attr: str, line: int) -> Value:
        """Look up an attribute of a module without storing it in the local namespace.

        For example, get_module_attr('typing', 'TypedDict', line) results in
        the value of 'typing.TypedDict'.

        Import the module if needed.
        """
        self.gen_import(module, line)
        module_obj = self.get_module(module, line)
        return self.py_get_attr(module_obj, attr, line)

    def assign_if_null(self, target: Register, get_val: Callable[[], Value], line: int) -> None:
        """If target is NULL, assign value produced by get_val to it."""
        error_block, body_block = BasicBlock(), BasicBlock()
        self.add(Branch(target, error_block, body_block, Branch.IS_ERROR))
        self.activate_block(error_block)
        self.add(Assign(target, self.coerce(get_val(), target.type, line)))
        self.goto(body_block)
        self.activate_block(body_block)

    def assign_if_bitmap_unset(
        self, target: Register, get_val: Callable[[], Value], index: int, line: int
    ) -> None:
        error_block, body_block = BasicBlock(), BasicBlock()
        o = self.int_op(
            bitmap_rprimitive,
            self.builder.args[-1 - index // BITMAP_BITS],
            Integer(1 << (index & (BITMAP_BITS - 1)), bitmap_rprimitive),
            IntOp.AND,
            line,
        )
        b = self.add(ComparisonOp(o, Integer(0, bitmap_rprimitive), ComparisonOp.EQ))
        self.add(Branch(b, error_block, body_block, Branch.BOOL))
        self.activate_block(error_block)
        self.add(Assign(target, self.coerce(get_val(), target.type, line)))
        self.goto(body_block)
        self.activate_block(body_block)

    def maybe_add_implicit_return(self) -> None:
        if is_none_rprimitive(self.ret_types[-1]) or is_object_rprimitive(self.ret_types[-1]):
            self.add_implicit_return()
        else:
            self.add_implicit_unreachable()

    def add_implicit_return(self) -> None:
        block = self.builder.blocks[-1]
        if not block.terminated:
            retval = self.coerce(self.builder.none(), self.ret_types[-1], -1)
            self.nonlocal_control[-1].gen_return(self, retval, self.fn_info.fitem.line)

    def add_implicit_unreachable(self) -> None:
        block = self.builder.blocks[-1]
        if not block.terminated:
            self.add(Unreachable())

    def disallow_class_assignments(self, lvalues: list[Lvalue], line: int) -> None:
        # Some best-effort attempts to disallow assigning to class
        # variables that aren't marked ClassVar, since we blatantly
        # miscompile the interaction between instance and class
        # variables.
        for lvalue in lvalues:
            if (
                isinstance(lvalue, MemberExpr)
                and isinstance(lvalue.expr, RefExpr)
                and isinstance(lvalue.expr.node, TypeInfo)
            ):
                var = lvalue.expr.node[lvalue.name].node
                if isinstance(var, Var) and not var.is_classvar:
                    self.error("Only class variables defined as ClassVar can be assigned to", line)

    def non_function_scope(self) -> bool:
        # Currently the stack always has at least two items: dummy and top-level.
        return len(self.fn_infos) <= 2

    def top_level_fn_info(self) -> FuncInfo | None:
        if self.non_function_scope():
            return None
        return self.fn_infos[2]

    def init_final_static(
        self,
        lvalue: Lvalue,
        rvalue_reg: Value,
        class_name: str | None = None,
        *,
        type_override: RType | None = None,
    ) -> None:
        assert isinstance(lvalue, NameExpr)
        assert isinstance(lvalue.node, Var)
        if lvalue.node.final_value is None:
            if class_name is None:
                name = lvalue.name
            else:
                name = f"{class_name}.{lvalue.name}"
            assert name is not None, "Full name not set for variable"
            coerced = self.coerce(rvalue_reg, type_override or self.node_type(lvalue), lvalue.line)
            self.final_names.append((name, coerced.type))
            self.add(InitStatic(coerced, name, self.module_name))

    def load_final_static(
        self, fullname: str, typ: RType, line: int, error_name: str | None = None
    ) -> Value:
        split_name = split_target(self.graph, fullname)
        assert split_name is not None
        module, name = split_name
        return self.builder.load_static_checked(
            typ,
            name,
            module,
            line=line,
            error_msg=f'value for final name "{error_name}" was not set',
        )

    def init_type_var(self, value: Value, name: str, line: int) -> None:
        unique_name = name + "___" + str(line)
        self.type_var_names.append(unique_name)
        self.add(InitStatic(value, unique_name, self.module_name, namespace=NAMESPACE_TYPE_VAR))

    def load_type_var(self, name: str, line: int) -> Value:
        return self.add(
            LoadStatic(
                object_rprimitive,
                name + "___" + str(line),
                self.module_name,
                namespace=NAMESPACE_TYPE_VAR,
            )
        )

    def load_literal_value(self, val: int | str | bytes | float | complex | bool) -> Value:
        """Load value of a final name, class-level attribute, or constant folded expression."""
        if isinstance(val, bool):
            if val:
                return self.true()
            else:
                return self.false()
        elif isinstance(val, int):
            return self.builder.load_int(val)
        elif isinstance(val, float):
            return self.builder.load_float(val)
        elif isinstance(val, str):
            return self.builder.load_str(val)
        elif isinstance(val, bytes):
            return self.builder.load_bytes(val)
        elif isinstance(val, complex):
            return self.builder.load_complex(val)
        else:
            assert False, "Unsupported literal value"

    def get_assignment_target(
        self, lvalue: Lvalue, line: int = -1, *, for_read: bool = False
    ) -> AssignmentTarget:
        if line == -1:
            line = lvalue.line
        if isinstance(lvalue, NameExpr):
            # If we are visiting a decorator, then the SymbolNode we really want to be looking at
            # is the function that is decorated, not the entire Decorator node itself.
            symbol = lvalue.node
            if isinstance(symbol, Decorator):
                symbol = symbol.func
            if symbol is None:
                # Semantic analyzer doesn't create ad-hoc Vars for special forms.
                assert lvalue.is_special_form
                symbol = Var(lvalue.name)
            if not for_read and isinstance(symbol, Var) and symbol.is_cls:
                self.error("Cannot assign to the first argument of classmethod", line)
            if lvalue.kind == LDEF:
                if symbol not in self.symtables[-1]:
                    if isinstance(symbol, Var) and not isinstance(symbol.type, DeletedType):
                        reg_type = self.type_to_rtype(symbol.type)
                    else:
                        reg_type = self.node_type(lvalue)
                    # If the function is a generator function, then first define a new variable
                    # in the current function's environment class. Next, define a target that
                    # refers to the newly defined variable in that environment class. Add the
                    # target to the table containing class environment variables, as well as the
                    # current environment.
                    if self.fn_info.is_generator:
                        return self.add_var_to_env_class(
                            symbol, reg_type, self.fn_info.generator_class, reassign=False
                        )

                    # Otherwise define a new local variable.
                    return self.add_local_reg(symbol, reg_type)
                else:
                    # Assign to a previously defined variable.
                    return self.lookup(symbol)
            elif lvalue.kind == GDEF:
                globals_dict = self.load_globals_dict()
                name = self.load_str(lvalue.name)
                return AssignmentTargetIndex(globals_dict, name)
            else:
                assert False, lvalue.kind
        elif isinstance(lvalue, IndexExpr):
            # Indexed assignment x[y] = e
            base = self.accept(lvalue.base)
            index = self.accept(lvalue.index)
            return AssignmentTargetIndex(base, index)
        elif isinstance(lvalue, MemberExpr):
            # Attribute assignment x.y = e
            can_borrow = self.is_native_attr_ref(lvalue)
            obj = self.accept(lvalue.expr, can_borrow=can_borrow)
            return AssignmentTargetAttr(obj, lvalue.name, can_borrow=can_borrow)
        elif isinstance(lvalue, TupleExpr):
            # Multiple assignment a, ..., b = e
            star_idx: int | None = None
            lvalues = []
            for idx, item in enumerate(lvalue.items):
                targ = self.get_assignment_target(item)
                lvalues.append(targ)
                if isinstance(item, StarExpr):
                    if star_idx is not None:
                        self.error("Two starred expressions in assignment", line)
                    star_idx = idx

            return AssignmentTargetTuple(lvalues, star_idx)

        elif isinstance(lvalue, StarExpr):
            return self.get_assignment_target(lvalue.expr)

        assert False, "Unsupported lvalue: %r" % lvalue

    def read(
        self, target: Value | AssignmentTarget, line: int = -1, can_borrow: bool = False
    ) -> Value:
        if isinstance(target, Value):
            return target
        if isinstance(target, AssignmentTargetRegister):
            return target.register
        if isinstance(target, AssignmentTargetIndex):
            reg = self.gen_method_call(
                target.base, "__getitem__", [target.index], target.type, line
            )
            if reg is not None:
                return reg
            assert False, target.base.type
        if isinstance(target, AssignmentTargetAttr):
            if isinstance(target.obj.type, RInstance) and target.obj.type.class_ir.is_ext_class:
                borrow = can_borrow and target.can_borrow
                return self.add(GetAttr(target.obj, target.attr, line, borrow=borrow))
            else:
                return self.py_get_attr(target.obj, target.attr, line)

        assert False, "Unsupported lvalue: %r" % target

    def read_nullable_attr(self, obj: Value, attr: str, line: int = -1) -> Value:
        """Read an attribute that might have an error value without raising AttributeError."""
        assert isinstance(obj.type, RInstance) and obj.type.class_ir.is_ext_class
        return self.add(GetAttr(obj, attr, line, allow_error_value=True))

    def assign(self, target: Register | AssignmentTarget, rvalue_reg: Value, line: int) -> None:
        if isinstance(target, Register):
            self.add(Assign(target, self.coerce_rvalue(rvalue_reg, target.type, line)))
        elif isinstance(target, AssignmentTargetRegister):
            rvalue_reg = self.coerce_rvalue(rvalue_reg, target.type, line)
            self.add(Assign(target.register, rvalue_reg))
        elif isinstance(target, AssignmentTargetAttr):
            if isinstance(target.obj_type, RInstance):
                rvalue_reg = self.coerce_rvalue(rvalue_reg, target.type, line)
                self.add(SetAttr(target.obj, target.attr, rvalue_reg, line))
            else:
                key = self.load_str(target.attr)
                boxed_reg = self.builder.box(rvalue_reg)
                self.primitive_op(py_setattr_op, [target.obj, key, boxed_reg], line)
        elif isinstance(target, AssignmentTargetIndex):
            target_reg2 = self.gen_method_call(
                target.base, "__setitem__", [target.index, rvalue_reg], None, line
            )
            assert target_reg2 is not None, target.base.type
        elif isinstance(target, AssignmentTargetTuple):
            if isinstance(rvalue_reg.type, RTuple) and target.star_idx is None:
                rtypes = rvalue_reg.type.types
                assert len(rtypes) == len(target.items)
                for i in range(len(rtypes)):
                    item_value = self.add(TupleGet(rvalue_reg, i, line))
                    self.assign(target.items[i], item_value, line)
            elif (
                is_list_rprimitive(rvalue_reg.type) or is_tuple_rprimitive(rvalue_reg.type)
            ) and target.star_idx is None:
                self.process_sequence_assignment(target, rvalue_reg, line)
            else:
                self.process_iterator_tuple_assignment(target, rvalue_reg, line)
        else:
            assert False, "Unsupported assignment target"

    def coerce_rvalue(self, rvalue: Value, rtype: RType, line: int) -> Value:
        if is_float_rprimitive(rtype) and is_tagged(rvalue.type):
            typename = rvalue.type.short_name()
            if typename == "short_int":
                typename = "int"
            self.error(
                "Incompatible value representations in assignment "
                + f'(expression has type "{typename}", variable has type "float")',
                line,
            )
        return self.coerce(rvalue, rtype, line)

    def process_sequence_assignment(
        self, target: AssignmentTargetTuple, rvalue: Value, line: int
    ) -> None:
        """Process assignment like 'x, y = s', where s is a variable-length list or tuple."""
        # Check the length of sequence.
        expected_len = Integer(len(target.items), c_pyssize_t_rprimitive)
        self.builder.call_c(check_unpack_count_op, [rvalue, expected_len], line)

        # Read sequence items.
        values = []
        for i in range(len(target.items)):
            item = target.items[i]
            index = self.builder.load_int(i)
            if is_list_rprimitive(rvalue.type):
                item_value = self.primitive_op(list_get_item_unsafe_op, [rvalue, index], line)
            else:
                item_value = self.builder.gen_method_call(
                    rvalue, "__getitem__", [index], item.type, line
                )
            values.append(item_value)

        # Assign sequence items to the target lvalues.
        for lvalue, value in zip(target.items, values):
            self.assign(lvalue, value, line)

    def process_iterator_tuple_assignment_helper(
        self, litem: AssignmentTarget, ritem: Value, line: int
    ) -> None:
        error_block, ok_block = BasicBlock(), BasicBlock()
        self.add(Branch(ritem, error_block, ok_block, Branch.IS_ERROR))

        self.activate_block(error_block)
        self.add(
            RaiseStandardError(RaiseStandardError.VALUE_ERROR, "not enough values to unpack", line)
        )
        self.add(Unreachable())

        self.activate_block(ok_block)
        self.assign(litem, ritem, line)

    def process_iterator_tuple_assignment(
        self, target: AssignmentTargetTuple, rvalue_reg: Value, line: int
    ) -> None:
        iterator = self.primitive_op(iter_op, [rvalue_reg], line)

        # This may be the whole lvalue list if there is no starred value
        split_idx = target.star_idx if target.star_idx is not None else len(target.items)

        # Assign values before the first starred value
        for litem in target.items[:split_idx]:
            ritem = self.call_c(next_op, [iterator], line)
            error_block, ok_block = BasicBlock(), BasicBlock()
            self.add(Branch(ritem, error_block, ok_block, Branch.IS_ERROR))

            self.activate_block(error_block)
            self.add(
                RaiseStandardError(
                    RaiseStandardError.VALUE_ERROR, "not enough values to unpack", line
                )
            )
            self.add(Unreachable())

            self.activate_block(ok_block)

            self.assign(litem, ritem, line)

        # Assign the starred value and all values after it
        if target.star_idx is not None:
            post_star_vals = target.items[split_idx + 1 :]
            iter_list = self.primitive_op(to_list, [iterator], line)
            iter_list_len = self.builtin_len(iter_list, line)
            post_star_len = Integer(len(post_star_vals))
            condition = self.binary_op(post_star_len, iter_list_len, "<=", line)

            error_block, ok_block = BasicBlock(), BasicBlock()
            self.add(Branch(condition, ok_block, error_block, Branch.BOOL))

            self.activate_block(error_block)
            self.add(
                RaiseStandardError(
                    RaiseStandardError.VALUE_ERROR, "not enough values to unpack", line
                )
            )
            self.add(Unreachable())

            self.activate_block(ok_block)

            for litem in reversed(post_star_vals):
                ritem = self.primitive_op(list_pop_last, [iter_list], line)
                self.assign(litem, ritem, line)

            # Assign the starred value
            self.assign(target.items[target.star_idx], iter_list, line)

        # There is no starred value, so check if there are extra values in rhs that
        # have not been assigned.
        else:
            extra = self.call_c(next_op, [iterator], line)
            error_block, ok_block = BasicBlock(), BasicBlock()
            self.add(Branch(extra, ok_block, error_block, Branch.IS_ERROR))

            self.activate_block(error_block)
            self.add(
                RaiseStandardError(
                    RaiseStandardError.VALUE_ERROR, "too many values to unpack", line
                )
            )
            self.add(Unreachable())

            self.activate_block(ok_block)

    def push_loop_stack(self, continue_block: BasicBlock, break_block: BasicBlock) -> None:
        self.nonlocal_control.append(
            LoopNonlocalControl(self.nonlocal_control[-1], continue_block, break_block)
        )

    def pop_loop_stack(self) -> None:
        self.nonlocal_control.pop()

    def make_spill_target(self, type: RType) -> AssignmentTarget:
        """Moves a given Value instance into the generator class' environment class."""
        name = f"{TEMP_ATTR_NAME}{self.temp_counter}"
        self.temp_counter += 1
        target = self.add_var_to_env_class(Var(name), type, self.fn_info.generator_class)
        return target

    def spill(self, value: Value) -> AssignmentTarget:
        """Moves a given Value instance into the generator class' environment class."""
        target = self.make_spill_target(value.type)
        # Shouldn't be able to fail, so -1 for line
        self.assign(target, value, -1)
        return target

    def maybe_spill(self, value: Value) -> Value | AssignmentTarget:
        """
        Moves a given Value instance into the environment class for generator functions. For
        non-generator functions, leaves the Value instance as it is.

        Returns an AssignmentTarget associated with the Value for generator functions and the
        original Value itself for non-generator functions.
        """
        if self.fn_info.is_generator:
            return self.spill(value)
        return value

    def maybe_spill_assignable(self, value: Value) -> Register | AssignmentTarget:
        """
        Moves a given Value instance into the environment class for generator functions. For
        non-generator functions, allocate a temporary Register.

        Returns an AssignmentTarget associated with the Value for generator functions and an
        assignable Register for non-generator functions.
        """
        if self.fn_info.is_generator:
            return self.spill(value)

        if isinstance(value, Register):
            return value

        # Allocate a temporary register for the assignable value.
        reg = Register(value.type)
        self.assign(reg, value, -1)
        return reg

    def extract_int(self, e: Expression) -> int | None:
        if isinstance(e, IntExpr):
            return e.value
        elif isinstance(e, UnaryExpr) and e.op == "-" and isinstance(e.expr, IntExpr):
            return -e.expr.value
        else:
            return None

    def get_sequence_type(self, expr: Expression) -> RType:
        return self.get_sequence_type_from_type(self.types[expr])

    def get_sequence_type_from_type(self, target_type: Type) -> RType:
        target_type = get_proper_type(target_type)
        if isinstance(target_type, UnionType):
            return RUnion.make_simplified_union(
                [self.get_sequence_type_from_type(item) for item in target_type.items]
            )
        elif isinstance(target_type, Instance):
            if target_type.type.fullname == "builtins.str":
                return str_rprimitive
            else:
                return self.type_to_rtype(target_type.args[0])
        # This elif-blocks are needed for iterating over classes derived from NamedTuple.
        elif isinstance(target_type, TypeVarLikeType):
            return self.get_sequence_type_from_type(target_type.upper_bound)
        elif isinstance(target_type, TupleType):
            # Tuple might have elements of different types.
            rtypes = {self.mapper.type_to_rtype(item) for item in target_type.items}
            if len(rtypes) == 1:
                return rtypes.pop()
            else:
                return RUnion.make_simplified_union(list(rtypes))
        assert False, target_type

    def get_dict_base_type(self, expr: Expression) -> list[Instance]:
        """Find dict type of a dict-like expression.

        This is useful for dict subclasses like SymbolTable.
        """
        return self.get_dict_base_type_from_type(self.types[expr])

    def get_dict_base_type_from_type(self, target_type: Type) -> list[Instance]:
        target_type = get_proper_type(target_type)
        if isinstance(target_type, UnionType):
            return [
                inner
                for item in target_type.items
                for inner in self.get_dict_base_type_from_type(item)
            ]
        if isinstance(target_type, TypeVarLikeType):
            # Match behaviour of self.node_type
            # We can only reach this point if `target_type` was a TypeVar(bound=dict[...])
            # or a ParamSpec.
            return self.get_dict_base_type_from_type(target_type.upper_bound)

        if isinstance(target_type, TypedDictType):
            target_type = target_type.fallback
            dict_base = next(
                base for base in target_type.type.mro if base.fullname == "typing.Mapping"
            )
        elif isinstance(target_type, Instance):
            dict_base = next(
                base for base in target_type.type.mro if base.fullname == "builtins.dict"
            )
        else:
            assert False, f"Failed to extract dict base from {target_type}"
        return [map_instance_to_supertype(target_type, dict_base)]

    def get_dict_key_type(self, expr: Expression) -> RType:
        dict_base_types = self.get_dict_base_type(expr)
        rtypes = [self.type_to_rtype(t.args[0]) for t in dict_base_types]
        return RUnion.make_simplified_union(rtypes)

    def get_dict_value_type(self, expr: Expression) -> RType:
        dict_base_types = self.get_dict_base_type(expr)
        rtypes = [self.type_to_rtype(t.args[1]) for t in dict_base_types]
        return RUnion.make_simplified_union(rtypes)

    def get_dict_item_type(self, expr: Expression) -> RType:
        key_type = self.get_dict_key_type(expr)
        value_type = self.get_dict_value_type(expr)
        return RTuple([key_type, value_type])

    def _analyze_iterable_item_type(self, expr: Expression) -> Type:
        """Return the item type given by 'expr' in an iterable context."""
        # This logic is copied from mypy's TypeChecker.analyze_iterable_item_type.
        if expr not in self.types:
            # Mypy thinks this is unreachable.
            iterable: ProperType = AnyType(TypeOfAny.from_error)
        else:
            iterable = get_proper_type(self.types[expr])
        echk = self.graph[self.module_name].type_checker().expr_checker
        iterator = echk.check_method_call_by_name("__iter__", iterable, [], [], expr)[0]

        from mypy.join import join_types

        if isinstance(iterable, TupleType):
            joined: Type = UninhabitedType()
            for item in iterable.items:
                joined = join_types(joined, item)
            return joined
        else:
            # Non-tuple iterable.
            return echk.check_method_call_by_name("__next__", iterator, [], [], expr)[0]

    def is_native_module(self, module: str) -> bool:
        """Is the given module one compiled by mypyc?"""
        return self.mapper.is_native_module(module)

    def is_native_ref_expr(self, expr: RefExpr) -> bool:
        return self.mapper.is_native_ref_expr(expr)

    def is_native_module_ref_expr(self, expr: RefExpr) -> bool:
        return self.mapper.is_native_module_ref_expr(expr)

    def is_synthetic_type(self, typ: TypeInfo) -> bool:
        """Is a type something other than just a class we've created?"""
        return typ.is_named_tuple or typ.is_newtype or typ.typeddict_type is not None

    def get_final_ref(self, expr: MemberExpr) -> tuple[str, Var, bool] | None:
        """Check if `expr` is a final attribute.

        This needs to be done differently for class and module attributes to
        correctly determine fully qualified name. Return a tuple that consists of
        the qualified name, the corresponding Var node, and a flag indicating whether
        the final name was defined in a compiled module. Return None if `expr` does not
        refer to a final attribute.
        """
        final_var = None
        if isinstance(expr.expr, RefExpr) and isinstance(expr.expr.node, TypeInfo):
            # a class attribute
            sym = expr.expr.node.get(expr.name)
            if sym and isinstance(sym.node, Var):
                # Enum attribute are treated as final since they are added to the global cache
                expr_fullname = expr.expr.node.bases[0].type.fullname
                is_final = sym.node.is_final or expr_fullname == "enum.Enum"
                if is_final:
                    final_var = sym.node
                    fullname = f"{sym.node.info.fullname}.{final_var.name}"
                    native = self.is_native_module(expr.expr.node.module_name)
        elif self.is_module_member_expr(expr):
            # a module attribute
            if isinstance(expr.node, Var) and expr.node.is_final:
                final_var = expr.node
                fullname = expr.node.fullname
                native = self.is_native_ref_expr(expr)
        if final_var is not None:
            return fullname, final_var, native
        return None

    def emit_load_final(
        self, final_var: Var, fullname: str, name: str, native: bool, typ: Type, line: int
    ) -> Value | None:
        """Emit code for loading value of a final name (if possible).

        Args:
            final_var: Var corresponding to the final name
            fullname: its qualified name
            name: shorter name to show in errors
            native: whether the name was defined in a compiled module
            typ: its type
            line: line number where loading occurs
        """
        if final_var.final_value is not None:  # this is safe even for non-native names
            return self.load_literal_value(final_var.final_value)
        elif native and module_prefix(self.graph, fullname):
            return self.load_final_static(fullname, self.mapper.type_to_rtype(typ), line, name)
        else:
            return None

    def is_module_member_expr(self, expr: MemberExpr) -> bool:
        return isinstance(expr.expr, RefExpr) and isinstance(expr.expr.node, MypyFile)

    def call_refexpr_with_args(
        self, expr: CallExpr, callee: RefExpr, arg_values: list[Value]
    ) -> Value:
        # Handle data-driven special-cased primitive call ops.
        if callee.fullname and expr.arg_kinds == [ARG_POS] * len(arg_values):
            fullname = get_call_target_fullname(callee)
            primitive_candidates = function_ops.get(fullname, [])
            target = self.builder.matching_primitive_op(
                primitive_candidates, arg_values, expr.line, self.node_type(expr)
            )
            if target:
                return target

        # Standard native call if signature and fullname are good and all arguments are positional
        # or named.
        callee_node = callee.node
        if isinstance(callee_node, OverloadedFuncDef):
            callee_node = callee_node.impl
        # TODO: use native calls for any decorated functions which have all their decorators
        # removed, not just singledispatch functions (which we don't do now just in case those
        # decorated functions are callable classes or cannot be called without the python API for
        # some other reason)
        if (
            isinstance(callee_node, Decorator)
            and callee_node.func not in self.fdefs_to_decorators
            and callee_node.func in self.singledispatch_impls
        ):
            callee_node = callee_node.func
        if (
            callee_node is not None
            and callee.fullname
            and callee_node in self.mapper.func_to_decl
            and all(kind in (ARG_POS, ARG_NAMED) for kind in expr.arg_kinds)
        ):
            decl = self.mapper.func_to_decl[callee_node]
            return self.builder.call(decl, arg_values, expr.arg_kinds, expr.arg_names, expr.line)

        # Fall back to a Python call
        function = self.accept(callee)
        return self.py_call(
            function, arg_values, expr.line, arg_kinds=expr.arg_kinds, arg_names=expr.arg_names
        )

    def shortcircuit_expr(self, expr: OpExpr) -> Value:
        def handle_right() -> Value:
            if expr.right_unreachable:
                self.builder.add(
                    RaiseStandardError(
                        RaiseStandardError.RUNTIME_ERROR,
                        "mypyc internal error: should be unreachable",
                        expr.right.line,
                    )
                )
                return self.builder.none()
            return self.accept(expr.right)

        return self.builder.shortcircuit_helper(
            expr.op, self.node_type(expr), lambda: self.accept(expr.left), handle_right, expr.line
        )

    # Basic helpers

    def flatten_classes(self, arg: RefExpr | TupleExpr) -> list[ClassIR] | None:
        """Flatten classes in isinstance(obj, (A, (B, C))).

        If at least one item is not a reference to a native class, return None.
        """
        if isinstance(arg, RefExpr):
            if isinstance(arg.node, TypeInfo) and self.is_native_module_ref_expr(arg):
                ir = self.mapper.type_to_ir.get(arg.node)
                if ir:
                    return [ir]
            return None
        else:
            res: list[ClassIR] = []
            for item in arg.items:
                if isinstance(item, (RefExpr, TupleExpr)):
                    item_part = self.flatten_classes(item)
                    if item_part is None:
                        return None
                    res.extend(item_part)
                else:
                    return None
            return res

    def enter(self, fn_info: FuncInfo | str = "", *, ret_type: RType = none_rprimitive) -> None:
        if isinstance(fn_info, str):
            fn_info = FuncInfo(name=fn_info)
        self.builder = LowLevelIRBuilder(self.errors, self.options)
        self.builder.set_module(self.module_name, self.module_path)
        self.builders.append(self.builder)
        self.symtables.append({})
        self.runtime_args.append([])
        self.fn_info = fn_info
        self.fn_infos.append(self.fn_info)
        self.ret_types.append(ret_type)
        if fn_info.is_generator:
            self.nonlocal_control.append(GeneratorNonlocalControl())
        else:
            self.nonlocal_control.append(BaseNonlocalControl())
        self.activate_block(BasicBlock())

    def leave(self) -> tuple[list[Register], list[RuntimeArg], list[BasicBlock], RType, FuncInfo]:
        builder = self.builders.pop()
        self.symtables.pop()
        runtime_args = self.runtime_args.pop()
        ret_type = self.ret_types.pop()
        fn_info = self.fn_infos.pop()
        self.nonlocal_control.pop()
        self.builder = self.builders[-1]
        self.fn_info = self.fn_infos[-1]
        return builder.args, runtime_args, builder.blocks, ret_type, fn_info

    @contextmanager
    def enter_method(
        self,
        class_ir: ClassIR,
        name: str,
        ret_type: RType,
        fn_info: FuncInfo | str = "",
        self_type: RType | None = None,
    ) -> Iterator[None]:
        """Generate IR for a method.

        If the method takes arguments, you should immediately afterwards call
        add_argument() for each non-self argument (self is created implicitly).

        Args:
            class_ir: Add method to this class
            name: Short name of the method
            ret_type: Return type of the method
            fn_info: Optionally, additional information about the method
            self_type: If not None, override default type of the implicit 'self'
                argument (by default, derive type from class_ir)
        """
        self.enter(fn_info, ret_type=ret_type)
        self.function_name_stack.append(name)
        self.class_ir_stack.append(class_ir)
        if self_type is None:
            self_type = RInstance(class_ir)
        self.add_argument(SELF_NAME, self_type)
        try:
            yield
        finally:
            arg_regs, args, blocks, ret_type, fn_info = self.leave()
            sig = FuncSignature(args, ret_type)
            name = self.function_name_stack.pop()
            class_ir = self.class_ir_stack.pop()
            decl = FuncDecl(name, class_ir.name, self.module_name, sig)
            ir = FuncIR(decl, arg_regs, blocks)
            class_ir.methods[name] = ir
            class_ir.method_decls[name] = ir.decl
            self.functions.append(ir)

    def add_argument(self, var: str | Var, typ: RType, kind: ArgKind = ARG_POS) -> Register:
        """Declare an argument in the current function.

        You should use this instead of directly calling add_local() in new code.
        """
        if isinstance(var, str):
            var = Var(var)
        reg = self.add_local(var, typ, is_arg=True)
        self.runtime_args[-1].append(RuntimeArg(var.name, typ, kind))
        return reg

    def lookup(self, symbol: SymbolNode) -> SymbolTarget:
        return self.symtables[-1][symbol]

    def add_local(self, symbol: SymbolNode, typ: RType, is_arg: bool = False) -> Register:
        """Add register that represents a symbol to the symbol table.

        Args:
            is_arg: is this a function argument
        """
        assert isinstance(symbol, SymbolNode)
        reg = Register(
            typ, remangle_redefinition_name(symbol.name), is_arg=is_arg, line=symbol.line
        )
        self.symtables[-1][symbol] = AssignmentTargetRegister(reg)
        if is_arg:
            self.builder.args.append(reg)
        return reg

    def add_local_reg(
        self, symbol: SymbolNode, typ: RType, is_arg: bool = False
    ) -> AssignmentTargetRegister:
        """Like add_local, but return an assignment target instead of value."""
        self.add_local(symbol, typ, is_arg)
        target = self.symtables[-1][symbol]
        assert isinstance(target, AssignmentTargetRegister)
        return target

    def add_self_to_env(self, cls: ClassIR) -> AssignmentTargetRegister:
        """Low-level function that adds a 'self' argument.

        This is only useful if using enter() instead of enter_method().
        """
        return self.add_local_reg(Var(SELF_NAME), RInstance(cls), is_arg=True)

    def add_target(self, symbol: SymbolNode, target: SymbolTarget) -> SymbolTarget:
        self.symtables[-1][symbol] = target
        return target

    def type_to_rtype(self, typ: Type | None) -> RType:
        return self.mapper.type_to_rtype(typ)

    def node_type(self, node: Expression) -> RType:
        if isinstance(node, IntExpr):
            # TODO: Don't special case IntExpr
            return int_rprimitive
        if node not in self.types:
            return object_rprimitive
        mypy_type = self.types[node]
        return self.type_to_rtype(mypy_type)

    def add_var_to_env_class(
        self,
        var: SymbolNode,
        rtype: RType,
        base: FuncInfo | ImplicitClass,
        reassign: bool = False,
        always_defined: bool = False,
    ) -> AssignmentTarget:
        # First, define the variable name as an attribute of the environment class, and then
        # construct a target for that attribute.
        name = remangle_redefinition_name(var.name)
        self.fn_info.env_class.attributes[name] = rtype
        if always_defined:
            self.fn_info.env_class.attrs_with_defaults.add(name)
        attr_target = AssignmentTargetAttr(base.curr_env_reg, name)

        if reassign:
            # Read the local definition of the variable, and set the corresponding attribute of
            # the environment class' variable to be that value.
            reg = self.read(self.lookup(var), self.fn_info.fitem.line)
            self.add(SetAttr(base.curr_env_reg, name, reg, self.fn_info.fitem.line))

        # Override the local definition of the variable to instead point at the variable in
        # the environment class.
        return self.add_target(var, attr_target)

    def is_builtin_ref_expr(self, expr: RefExpr) -> bool:
        assert expr.node, "RefExpr not resolved"
        return "." in expr.node.fullname and expr.node.fullname.split(".")[0] == "builtins"

    def load_global(self, expr: NameExpr) -> Value:
        """Loads a Python-level global.

        This takes a NameExpr and uses its name as a key to retrieve the corresponding PyObject *
        from the _globals dictionary in the C-generated code.
        """
        # If the global is from 'builtins', turn it into a module attr load instead
        if self.is_builtin_ref_expr(expr):
            assert expr.node, "RefExpr not resolved"
            return self.load_module_attr_by_fullname(expr.node.fullname, expr.line)
        if (
            self.is_native_module_ref_expr(expr)
            and isinstance(expr.node, TypeInfo)
            and not self.is_synthetic_type(expr.node)
        ):
            assert expr.fullname
            return self.load_native_type_object(expr.fullname)
        return self.load_global_str(expr.name, expr.line)

    def load_global_str(self, name: str, line: int) -> Value:
        _globals = self.load_globals_dict()
        reg = self.load_str(name)
        return self.primitive_op(dict_get_item_op, [_globals, reg], line)

    def load_globals_dict(self) -> Value:
        return self.add(LoadStatic(dict_rprimitive, "globals", self.module_name))

    def load_module_attr_by_fullname(self, fullname: str, line: int) -> Value:
        module, _, name = fullname.rpartition(".")
        left = self.load_module(module)
        return self.py_get_attr(left, name, line)

    def is_native_attr_ref(self, expr: MemberExpr) -> bool:
        """Is expr a direct reference to a native (struct) attribute of an instance?"""
        obj_rtype = self.node_type(expr.expr)
        return (
            isinstance(obj_rtype, RInstance)
            and obj_rtype.class_ir.is_ext_class
            and obj_rtype.class_ir.has_attr(expr.name)
            and not obj_rtype.class_ir.get_method(expr.name)
        )

    def mark_block_unreachable(self) -> None:
        """Mark statements in the innermost block being processed as unreachable.

        This should be called after a statement that unconditionally leaves the
        block, such as 'break' or 'return'.
        """
        self.block_reachable_stack[-1] = False

    # Lacks a good type because there wasn't a reasonable type in 3.5 :(
    def catch_errors(self, line: int) -> Any:
        return catch_errors(self.module_path, line)

    def warning(self, msg: str, line: int) -> None:
        self.errors.warning(msg, self.module_path, line)

    def error(self, msg: str, line: int) -> None:
        self.errors.error(msg, self.module_path, line)

    def note(self, msg: str, line: int) -> None:
        self.errors.note(msg, self.module_path, line)

    def add_function(self, func_ir: FuncIR, line: int) -> None:
        name = (func_ir.class_name, func_ir.name)
        if name in self.function_names:
            self.error(f'Duplicate definition of "{name[1]}" not supported by mypyc', line)
            return
        self.function_names.add(name)
        self.functions.append(func_ir)


def gen_arg_defaults(builder: IRBuilder) -> None:
    """Generate blocks for arguments that have default values.

    If the passed value is an error value, then assign the default
    value to the argument.
    """
    fitem = builder.fn_info.fitem
    nb = 0
    for arg in fitem.arguments:
        if arg.initializer:
            target = builder.lookup(arg.variable)

            def get_default() -> Value:
                assert arg.initializer is not None

                # If it is constant, don't bother storing it
                if is_constant(arg.initializer):
                    return builder.accept(arg.initializer)

                # Because gen_arg_defaults runs before calculate_arg_defaults, we
                # add the static/attribute to final_names/the class here.
                elif not builder.fn_info.is_nested:
                    name = fitem.fullname + "." + arg.variable.name
                    builder.final_names.append((name, target.type))
                    return builder.add(LoadStatic(target.type, name, builder.module_name))
                else:
                    name = arg.variable.name
                    builder.fn_info.callable_class.ir.attributes[name] = target.type
                    return builder.add(
                        GetAttr(builder.fn_info.callable_class.self_reg, name, arg.line)
                    )

            assert isinstance(target, AssignmentTargetRegister)
            reg = target.register
            if not reg.type.error_overlap:
                builder.assign_if_null(target.register, get_default, arg.initializer.line)
            else:
                builder.assign_if_bitmap_unset(
                    target.register, get_default, nb, arg.initializer.line
                )
                nb += 1


def remangle_redefinition_name(name: str) -> str:
    """Remangle names produced by mypy when allow-redefinition is used and a name
    is used with multiple types within a single block.

    We only need to do this for locals, because the name is used as the name of the register;
    for globals, the name itself is stored in a register for the purpose of doing dict
    lookups.
    """
    return name.replace("'", "__redef__")


def get_call_target_fullname(ref: RefExpr) -> str:
    if isinstance(ref.node, TypeAlias):
        # Resolve simple type aliases. In calls they evaluate to the type they point to.
        target = get_proper_type(ref.node.target)
        if isinstance(target, Instance):
            return target.type.fullname
    return ref.fullname


def create_type_params(
    builder: IRBuilder, typing_mod: Value, type_args: list[TypeParam], line: int
) -> list[Value]:
    """Create objects representing various kinds of Python 3.12 type parameters.

    The "typing_mod" argument is the "_typing" module object. The type objects
    are looked up from it.

    The returned list has one item for each "type_args" item, in the same order.
    Each item is either a TypeVar, TypeVarTuple or ParamSpec instance.
    """
    tvs = []
    type_var_imported: Value | None = None
    for type_param in type_args:
        if type_param.kind == TYPE_VAR_KIND:
            if type_var_imported:
                # Reuse previously imported value as a minor optimization
                tvt = type_var_imported
            else:
                tvt = builder.py_get_attr(typing_mod, "TypeVar", line)
                type_var_imported = tvt
        elif type_param.kind == TYPE_VAR_TUPLE_KIND:
            tvt = builder.py_get_attr(typing_mod, "TypeVarTuple", line)
        else:
            assert type_param.kind == PARAM_SPEC_KIND
            tvt = builder.py_get_attr(typing_mod, "ParamSpec", line)
        if type_param.kind != TYPE_VAR_TUPLE_KIND:
            # To match runtime semantics, pass infer_variance=True
            tv = builder.py_call(
                tvt,
                [builder.load_str(type_param.name), builder.true()],
                line,
                arg_kinds=[ARG_POS, ARG_NAMED],
                arg_names=[None, "infer_variance"],
            )
        else:
            tv = builder.py_call(tvt, [builder.load_str(type_param.name)], line)
        builder.init_type_var(tv, type_param.name, line)
        tvs.append(tv)
    return tvs


def calculate_arg_defaults(
    builder: IRBuilder,
    fn_info: FuncInfo,
    func_reg: Value | None,
    symtable: dict[SymbolNode, SymbolTarget],
) -> None:
    """Calculate default argument values and store them.

    They are stored in statics for top level functions and in
    the function objects for nested functions (while constants are
    still stored computed on demand).
    """
    fitem = fn_info.fitem
    for arg in fitem.arguments:
        # Constant values don't get stored but just recomputed
        if arg.initializer and not is_constant(arg.initializer):
            value = builder.coerce(
                builder.accept(arg.initializer), symtable[arg.variable].type, arg.line
            )
            if not fn_info.is_nested:
                name = fitem.fullname + "." + arg.variable.name
                builder.add(InitStatic(value, name, builder.module_name))
            else:
                assert func_reg is not None
                builder.add(SetAttr(func_reg, arg.variable.name, value, arg.line))
