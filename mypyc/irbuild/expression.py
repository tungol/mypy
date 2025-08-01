"""Transform mypy expression ASTs to mypyc IR (Intermediate Representation).

The top-level AST transformation logic is implemented in mypyc.irbuild.visitor
and mypyc.irbuild.builder.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Callable

from mypy.nodes import (
    ARG_NAMED,
    ARG_POS,
    LDEF,
    AssertTypeExpr,
    AssignmentExpr,
    BytesExpr,
    CallExpr,
    CastExpr,
    ComparisonExpr,
    ComplexExpr,
    ConditionalExpr,
    DictExpr,
    DictionaryComprehension,
    EllipsisExpr,
    Expression,
    FloatExpr,
    GeneratorExpr,
    IndexExpr,
    IntExpr,
    ListComprehension,
    ListExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    OpExpr,
    RefExpr,
    SetComprehension,
    SetExpr,
    SliceExpr,
    StarExpr,
    StrExpr,
    SuperExpr,
    TupleExpr,
    TypeApplication,
    TypeInfo,
    TypeVarLikeExpr,
    UnaryExpr,
    Var,
)
from mypy.types import Instance, ProperType, TupleType, TypeType, get_proper_type
from mypyc.common import MAX_SHORT_INT
from mypyc.ir.class_ir import ClassIR
from mypyc.ir.func_ir import FUNC_CLASSMETHOD, FUNC_STATICMETHOD
from mypyc.ir.ops import (
    Assign,
    BasicBlock,
    ComparisonOp,
    Integer,
    LoadAddress,
    LoadLiteral,
    PrimitiveDescription,
    RaiseStandardError,
    Register,
    TupleGet,
    TupleSet,
    Value,
)
from mypyc.ir.rtypes import (
    RTuple,
    bool_rprimitive,
    int_rprimitive,
    is_fixed_width_rtype,
    is_int_rprimitive,
    is_list_rprimitive,
    is_none_rprimitive,
    object_rprimitive,
    set_rprimitive,
)
from mypyc.irbuild.ast_helpers import is_borrow_friendly_expr, process_conditional
from mypyc.irbuild.builder import IRBuilder, int_borrow_friendly_op
from mypyc.irbuild.constant_fold import constant_fold_expr
from mypyc.irbuild.for_helpers import (
    comprehension_helper,
    raise_error_if_contains_unreachable_names,
    translate_list_comprehension,
    translate_set_comprehension,
)
from mypyc.irbuild.format_str_tokenizer import (
    convert_format_expr_to_bytes,
    convert_format_expr_to_str,
    join_formatted_bytes,
    join_formatted_strings,
    tokenizer_printf_style,
)
from mypyc.irbuild.specialize import apply_function_specialization, apply_method_specialization
from mypyc.primitives.bytes_ops import bytes_slice_op
from mypyc.primitives.dict_ops import dict_get_item_op, dict_new_op, dict_set_item_op
from mypyc.primitives.generic_ops import iter_op
from mypyc.primitives.list_ops import list_append_op, list_extend_op, list_slice_op
from mypyc.primitives.misc_ops import ellipsis_op, get_module_dict_op, new_slice_op, type_op
from mypyc.primitives.registry import builtin_names
from mypyc.primitives.set_ops import set_add_op, set_in_op, set_update_op
from mypyc.primitives.str_ops import str_slice_op
from mypyc.primitives.tuple_ops import list_tuple_op, tuple_slice_op

# Name and attribute references


def transform_name_expr(builder: IRBuilder, expr: NameExpr) -> Value:
    if isinstance(expr.node, TypeVarLikeExpr) and expr.node.is_new_style:
        # Reference to Python 3.12 implicit TypeVar/TupleVarTuple/... object.
        # These are stored in C statics and not visible in Python namespaces.
        return builder.load_type_var(expr.node.name, expr.node.line)
    if expr.node is None:
        builder.add(
            RaiseStandardError(
                RaiseStandardError.NAME_ERROR, f'name "{expr.name}" is not defined', expr.line
            )
        )
        return builder.none()
    fullname = expr.node.fullname
    if fullname in builtin_names:
        typ, src = builtin_names[fullname]
        return builder.add(LoadAddress(typ, src, expr.line))
    # special cases
    if fullname == "builtins.None":
        return builder.none()
    if fullname == "builtins.True":
        return builder.true()
    if fullname == "builtins.False":
        return builder.false()
    if fullname in ("typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"):
        return builder.false()

    math_literal = transform_math_literal(builder, fullname)
    if math_literal is not None:
        return math_literal

    if isinstance(expr.node, Var) and expr.node.is_final:
        value = builder.emit_load_final(
            expr.node,
            fullname,
            expr.name,
            builder.is_native_ref_expr(expr),
            builder.types[expr],
            expr.line,
        )
        if value is not None:
            return value

    if isinstance(expr.node, MypyFile) and expr.node.fullname in builder.imports:
        return builder.load_module(expr.node.fullname)

    # If the expression is locally defined, then read the result from the corresponding
    # assignment target and return it. Otherwise if the expression is a global, load it from
    # the globals dictionary.
    # Except for imports, that currently always happens in the global namespace.
    if expr.kind == LDEF and not (isinstance(expr.node, Var) and expr.node.is_suppressed_import):
        # Try to detect and error when we hit the irritating mypy bug
        # where a local variable is cast to None. (#5423)
        if (
            isinstance(expr.node, Var)
            and is_none_rprimitive(builder.node_type(expr))
            and expr.node.is_inferred
        ):
            builder.error(
                'Local variable "{}" has inferred type None; add an annotation'.format(
                    expr.node.name
                ),
                expr.node.line,
            )

        # TODO: Behavior currently only defined for Var, FuncDef and MypyFile node types.
        if isinstance(expr.node, MypyFile):
            # Load reference to a module imported inside function from
            # the modules dictionary. It would be closer to Python
            # semantics to access modules imported inside functions
            # via local variables, but this is tricky since the mypy
            # AST doesn't include a Var node for the module. We
            # instead load the module separately on each access.
            mod_dict = builder.call_c(get_module_dict_op, [], expr.line)
            obj = builder.primitive_op(
                dict_get_item_op, [mod_dict, builder.load_str(expr.node.fullname)], expr.line
            )
            return obj
        else:
            return builder.read(builder.get_assignment_target(expr, for_read=True), expr.line)

    return builder.load_global(expr)


def transform_member_expr(builder: IRBuilder, expr: MemberExpr) -> Value:
    # Special Cases
    if expr.fullname in ("typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"):
        return builder.false()

    # First check if this is maybe a final attribute.
    final = builder.get_final_ref(expr)
    if final is not None:
        fullname, final_var, native = final
        value = builder.emit_load_final(
            final_var, fullname, final_var.name, native, builder.types[expr], expr.line
        )
        if value is not None:
            return value

    math_literal = transform_math_literal(builder, expr.fullname)
    if math_literal is not None:
        return math_literal

    if isinstance(expr.node, MypyFile) and expr.node.fullname in builder.imports:
        return builder.load_module(expr.node.fullname)

    can_borrow = builder.is_native_attr_ref(expr)
    obj = builder.accept(expr.expr, can_borrow=can_borrow)
    rtype = builder.node_type(expr)

    # Special case: for named tuples transform attribute access to faster index access.
    typ = get_proper_type(builder.types.get(expr.expr))
    if isinstance(typ, TupleType) and typ.partial_fallback.type.is_named_tuple:
        fields = typ.partial_fallback.type.metadata["namedtuple"]["fields"]
        if expr.name in fields:
            index = builder.builder.load_int(fields.index(expr.name))
            return builder.gen_method_call(obj, "__getitem__", [index], rtype, expr.line)

    check_instance_attribute_access_through_class(builder, expr, typ)

    borrow = can_borrow and builder.can_borrow
    return builder.builder.get_attr(obj, expr.name, rtype, expr.line, borrow=borrow)


def check_instance_attribute_access_through_class(
    builder: IRBuilder, expr: MemberExpr, typ: ProperType | None
) -> None:
    """Report error if accessing an instance attribute through class object."""
    if isinstance(expr.expr, RefExpr):
        node = expr.expr.node
        if isinstance(typ, TypeType) and isinstance(typ.item, Instance):
            # TODO: Handle other item types
            node = typ.item.type
        if isinstance(node, TypeInfo):
            class_ir = builder.mapper.type_to_ir.get(node)
            if class_ir is not None and class_ir.is_ext_class:
                sym = node.get(expr.name)
                if (
                    sym is not None
                    and isinstance(sym.node, Var)
                    and not sym.node.is_classvar
                    and not sym.node.is_final
                ):
                    builder.error(
                        'Cannot access instance attribute "{}" through class object'.format(
                            expr.name
                        ),
                        expr.line,
                    )
                    builder.note(
                        '(Hint: Use "x: Final = ..." or "x: ClassVar = ..." to define '
                        "a class attribute)",
                        expr.line,
                    )


def transform_super_expr(builder: IRBuilder, o: SuperExpr) -> Value:
    # warning(builder, 'can not optimize super() expression', o.line)
    sup_val = builder.load_module_attr_by_fullname("builtins.super", o.line)
    if o.call.args:
        args = [builder.accept(arg) for arg in o.call.args]
    else:
        assert o.info is not None
        typ = builder.load_native_type_object(o.info.fullname)
        ir = builder.mapper.type_to_ir[o.info]
        iter_env = iter(builder.builder.args)
        # Grab first argument
        vself: Value = next(iter_env)
        if builder.fn_info.is_generator:
            # grab sixth argument (see comment in translate_super_method_call)
            self_targ = list(builder.symtables[-1].values())[6]
            vself = builder.read(self_targ, builder.fn_info.fitem.line)
        elif not ir.is_ext_class:
            vself = next(iter_env)  # second argument is self if non_extension class
        args = [typ, vself]
    res = builder.py_call(sup_val, args, o.line)
    return builder.py_get_attr(res, o.name, o.line)


# Calls


def transform_call_expr(builder: IRBuilder, expr: CallExpr) -> Value:
    callee = expr.callee
    if isinstance(expr.analyzed, CastExpr):
        return translate_cast_expr(builder, expr.analyzed)
    elif isinstance(expr.analyzed, AssertTypeExpr):
        # Compile to a no-op.
        return builder.accept(expr.analyzed.expr)
    elif (
        isinstance(callee, (NameExpr, MemberExpr))
        and isinstance(callee.node, TypeInfo)
        and callee.node.is_newtype
    ):
        # A call to a NewType type is a no-op at runtime.
        return builder.accept(expr.args[0])

    if isinstance(callee, IndexExpr) and isinstance(callee.analyzed, TypeApplication):
        callee = callee.analyzed.expr  # Unwrap type application

    if isinstance(callee, MemberExpr):
        if isinstance(callee.expr, RefExpr) and isinstance(callee.expr.node, MypyFile):
            # Call a module-level function, not a method.
            return translate_call(builder, expr, callee)
        return apply_method_specialization(builder, expr, callee) or translate_method_call(
            builder, expr, callee
        )
    elif isinstance(callee, SuperExpr):
        return translate_super_method_call(builder, expr, callee)
    else:
        return translate_call(builder, expr, callee)


def translate_call(builder: IRBuilder, expr: CallExpr, callee: Expression) -> Value:
    # The common case of calls is refexprs
    if isinstance(callee, RefExpr):
        return apply_function_specialization(builder, expr, callee) or translate_refexpr_call(
            builder, expr, callee
        )

    function = builder.accept(callee)
    args = [builder.accept(arg) for arg in expr.args]
    return builder.py_call(
        function, args, expr.line, arg_kinds=expr.arg_kinds, arg_names=expr.arg_names
    )


def translate_refexpr_call(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value:
    """Translate a non-method call."""
    # Gen the argument values
    arg_values = [builder.accept(arg) for arg in expr.args]

    return builder.call_refexpr_with_args(expr, callee, arg_values)


def translate_method_call(builder: IRBuilder, expr: CallExpr, callee: MemberExpr) -> Value:
    """Generate IR for an arbitrary call of form e.m(...).

    This can also deal with calls to module-level functions.
    """
    if builder.is_native_ref_expr(callee):
        # Call to module-level native function or such
        return translate_call(builder, expr, callee)
    elif (
        isinstance(callee.expr, RefExpr)
        and isinstance(callee.expr.node, TypeInfo)
        and callee.expr.node in builder.mapper.type_to_ir
        and builder.mapper.type_to_ir[callee.expr.node].has_method(callee.name)
        and all(kind in (ARG_POS, ARG_NAMED) for kind in expr.arg_kinds)
    ):
        # Call a method via the *class*
        assert isinstance(callee.expr.node, TypeInfo)
        ir = builder.mapper.type_to_ir[callee.expr.node]
        return call_classmethod(builder, ir, expr, callee)
    elif builder.is_module_member_expr(callee):
        # Fall back to a PyCall for non-native module calls
        function = builder.accept(callee)
        args = [builder.accept(arg) for arg in expr.args]
        return builder.py_call(
            function, args, expr.line, arg_kinds=expr.arg_kinds, arg_names=expr.arg_names
        )
    else:
        if isinstance(callee.expr, RefExpr):
            node = callee.expr.node
            if isinstance(node, Var) and node.is_cls:
                typ = get_proper_type(node.type)
                if isinstance(typ, TypeType) and isinstance(typ.item, Instance):
                    class_ir = builder.mapper.type_to_ir.get(typ.item.type)
                    if class_ir and class_ir.is_ext_class and class_ir.has_no_subclasses():
                        # Call a native classmethod via cls that can be statically bound,
                        # since the class has no subclasses.
                        return call_classmethod(builder, class_ir, expr, callee)

        receiver_typ = builder.node_type(callee.expr)

        # If there is a specializer for this method name/type, try calling it.
        # We would return the first successful one.
        val = apply_method_specialization(builder, expr, callee, receiver_typ)
        if val is not None:
            return val

        obj = builder.accept(callee.expr)
        args = [builder.accept(arg) for arg in expr.args]
        return builder.gen_method_call(
            obj,
            callee.name,
            args,
            builder.node_type(expr),
            expr.line,
            expr.arg_kinds,
            expr.arg_names,
        )


def call_classmethod(builder: IRBuilder, ir: ClassIR, expr: CallExpr, callee: MemberExpr) -> Value:
    decl = ir.method_decl(callee.name)
    args = []
    arg_kinds, arg_names = expr.arg_kinds.copy(), expr.arg_names.copy()
    # Add the class argument for class methods in extension classes
    if decl.kind == FUNC_CLASSMETHOD and ir.is_ext_class:
        args.append(builder.load_native_type_object(ir.fullname))
        arg_kinds.insert(0, ARG_POS)
        arg_names.insert(0, None)
    args += [builder.accept(arg) for arg in expr.args]

    if ir.is_ext_class:
        return builder.builder.call(decl, args, arg_kinds, arg_names, expr.line)
    else:
        obj = builder.accept(callee.expr)
        return builder.gen_method_call(
            obj,
            callee.name,
            args,
            builder.node_type(expr),
            expr.line,
            expr.arg_kinds,
            expr.arg_names,
        )


def translate_super_method_call(builder: IRBuilder, expr: CallExpr, callee: SuperExpr) -> Value:
    if callee.info is None or (len(callee.call.args) != 0 and len(callee.call.args) != 2):
        return translate_call(builder, expr, callee)

    # We support two-argument super but only when it is super(CurrentClass, self)
    # TODO: We could support it when it is a parent class in many cases?
    if len(callee.call.args) == 2:
        self_arg = callee.call.args[1]
        if (
            not isinstance(self_arg, NameExpr)
            or not isinstance(self_arg.node, Var)
            or not self_arg.node.is_self
        ):
            return translate_call(builder, expr, callee)

        typ_arg = callee.call.args[0]
        if (
            not isinstance(typ_arg, NameExpr)
            or not isinstance(typ_arg.node, TypeInfo)
            or callee.info is not typ_arg.node
        ):
            return translate_call(builder, expr, callee)

    ir = builder.mapper.type_to_ir[callee.info]
    # Search for the method in the mro, skipping ourselves. We
    # determine targets of super calls to native methods statically.
    for base in ir.mro[1:]:
        if callee.name in base.method_decls:
            break
    else:
        if (
            ir.is_ext_class
            and ir.builtin_base is None
            and not ir.inherits_python
            and callee.name == "__init__"
            and len(expr.args) == 0
        ):
            # Call translates to object.__init__(self), which is a
            # no-op, so omit the call.
            return builder.none()
        return translate_call(builder, expr, callee)

    decl = base.method_decl(callee.name)
    arg_values = [builder.accept(arg) for arg in expr.args]
    arg_kinds, arg_names = expr.arg_kinds.copy(), expr.arg_names.copy()

    if decl.kind != FUNC_STATICMETHOD:
        # Grab first argument
        vself: Value = builder.self()
        if decl.kind == FUNC_CLASSMETHOD:
            vself = builder.primitive_op(type_op, [vself], expr.line)
        elif builder.fn_info.is_generator:
            # For generator classes, the self target is the 6th value
            # in the symbol table (which is an ordered dict). This is sort
            # of ugly, but we can't search by name since the 'self' parameter
            # could be named anything, and it doesn't get added to the
            # environment indexes.
            self_targ = list(builder.symtables[-1].values())[6]
            vself = builder.read(self_targ, builder.fn_info.fitem.line)
        arg_values.insert(0, vself)
        arg_kinds.insert(0, ARG_POS)
        arg_names.insert(0, None)

    return builder.builder.call(decl, arg_values, arg_kinds, arg_names, expr.line)


def translate_cast_expr(builder: IRBuilder, expr: CastExpr) -> Value:
    src = builder.accept(expr.expr)
    target_type = builder.type_to_rtype(expr.type)
    return builder.coerce(src, target_type, expr.line)


# Operators


def transform_unary_expr(builder: IRBuilder, expr: UnaryExpr) -> Value:
    folded = try_constant_fold(builder, expr)
    if folded:
        return folded

    return builder.unary_op(builder.accept(expr.expr), expr.op, expr.line)


def transform_op_expr(builder: IRBuilder, expr: OpExpr) -> Value:
    if expr.op in ("and", "or"):
        return builder.shortcircuit_expr(expr)

    # Special case for string formatting
    if expr.op == "%" and isinstance(expr.left, (StrExpr, BytesExpr)):
        ret = translate_printf_style_formatting(builder, expr.left, expr.right)
        if ret is not None:
            return ret

    folded = try_constant_fold(builder, expr)
    if folded:
        return folded

    borrow_left = False
    borrow_right = False

    ltype = builder.node_type(expr.left)
    rtype = builder.node_type(expr.right)

    # Special case some int ops to allow borrowing operands.
    if is_int_rprimitive(ltype) and is_int_rprimitive(rtype):
        if expr.op == "//":
            expr = try_optimize_int_floor_divide(expr)
        if expr.op in int_borrow_friendly_op:
            borrow_left = is_borrow_friendly_expr(builder, expr.right)
            borrow_right = True
    elif is_fixed_width_rtype(ltype) and is_fixed_width_rtype(rtype):
        borrow_left = is_borrow_friendly_expr(builder, expr.right)
        borrow_right = True

    left = builder.accept(expr.left, can_borrow=borrow_left)
    right = builder.accept(expr.right, can_borrow=borrow_right)
    return builder.binary_op(left, right, expr.op, expr.line)


def try_optimize_int_floor_divide(expr: OpExpr) -> OpExpr:
    """Replace // with a power of two with a right shift, if possible."""
    if not isinstance(expr.right, IntExpr):
        return expr
    divisor = expr.right.value
    shift = divisor.bit_length() - 1
    if 0 < shift < 28 and divisor == (1 << shift):
        return OpExpr(">>", expr.left, IntExpr(shift))
    return expr


def transform_index_expr(builder: IRBuilder, expr: IndexExpr) -> Value:
    index = expr.index
    base_type = builder.node_type(expr.base)
    is_list = is_list_rprimitive(base_type)
    can_borrow_base = is_list and is_borrow_friendly_expr(builder, index)

    base = builder.accept(expr.base, can_borrow=can_borrow_base)

    if isinstance(base.type, RTuple) and isinstance(index, IntExpr):
        return builder.add(TupleGet(base, index.value, expr.line))

    if isinstance(index, SliceExpr):
        value = try_gen_slice_op(builder, base, index)
        if value:
            return value

    index_reg = builder.accept(expr.index, can_borrow=is_list)
    return builder.gen_method_call(
        base, "__getitem__", [index_reg], builder.node_type(expr), expr.line
    )


def try_constant_fold(builder: IRBuilder, expr: Expression) -> Value | None:
    """Return the constant value of an expression if possible.

    Return None otherwise.
    """
    value = constant_fold_expr(builder, expr)
    if value is not None:
        return builder.load_literal_value(value)
    return None


def try_gen_slice_op(builder: IRBuilder, base: Value, index: SliceExpr) -> Value | None:
    """Generate specialized slice op for some index expressions.

    Return None if a specialized op isn't available.

    This supports obj[x:y], obj[:x], and obj[x:] for a few types.
    """
    if index.stride:
        # We can only handle the default stride of 1.
        return None

    if index.begin_index:
        begin_type = builder.node_type(index.begin_index)
    else:
        begin_type = int_rprimitive
    if index.end_index:
        end_type = builder.node_type(index.end_index)
    else:
        end_type = int_rprimitive

    # Both begin and end index must be int (or missing).
    if is_int_rprimitive(begin_type) and is_int_rprimitive(end_type):
        if index.begin_index:
            begin = builder.accept(index.begin_index)
        else:
            begin = builder.load_int(0)
        if index.end_index:
            end = builder.accept(index.end_index)
        else:
            # Replace missing end index with the largest short integer
            # (a sequence can't be longer).
            end = builder.load_int(MAX_SHORT_INT)
        candidates = [list_slice_op, tuple_slice_op, str_slice_op, bytes_slice_op]
        return builder.builder.matching_call_c(candidates, [base, begin, end], index.line)

    return None


def transform_conditional_expr(builder: IRBuilder, expr: ConditionalExpr) -> Value:
    if_body, else_body, next_block = BasicBlock(), BasicBlock(), BasicBlock()

    process_conditional(builder, expr.cond, if_body, else_body)
    expr_type = builder.node_type(expr)
    # Having actual Phi nodes would be really nice here!
    target = Register(expr_type)

    builder.activate_block(if_body)
    true_value = builder.accept(expr.if_expr)
    true_value = builder.coerce(true_value, expr_type, expr.line)
    builder.add(Assign(target, true_value))
    builder.goto(next_block)

    builder.activate_block(else_body)
    false_value = builder.accept(expr.else_expr)
    false_value = builder.coerce(false_value, expr_type, expr.line)
    builder.add(Assign(target, false_value))
    builder.goto(next_block)

    builder.activate_block(next_block)

    return target


def set_literal_values(builder: IRBuilder, items: Sequence[Expression]) -> list[object] | None:
    values: list[object] = []
    for item in items:
        const_value = constant_fold_expr(builder, item)
        if const_value is not None:
            values.append(const_value)
            continue

        if isinstance(item, RefExpr):
            if item.fullname == "builtins.None":
                values.append(None)
            elif item.fullname == "builtins.True":
                values.append(True)
            elif item.fullname == "builtins.False":
                values.append(False)
        elif isinstance(item, TupleExpr):
            tuple_values = set_literal_values(builder, item.items)
            if tuple_values is not None:
                values.append(tuple(tuple_values))

    if len(values) != len(items):
        # Bail if not all items can be converted into values.
        return None
    return values


def precompute_set_literal(builder: IRBuilder, s: SetExpr) -> Value | None:
    """Try to pre-compute a frozenset literal during module initialization.

    Return None if it's not possible.

    Supported items:
     - Anything supported by irbuild.constant_fold.constant_fold_expr()
     - None, True, and False
     - Tuple literals with only items listed above
    """
    values = set_literal_values(builder, s.items)
    if values is not None:
        return builder.add(LoadLiteral(frozenset(values), set_rprimitive))

    return None


def transform_comparison_expr(builder: IRBuilder, e: ComparisonExpr) -> Value:
    # x in (...)/[...]
    # x not in (...)/[...]
    first_op = e.operators[0]
    if (
        first_op in ["in", "not in"]
        and len(e.operators) == 1
        and isinstance(e.operands[1], (TupleExpr, ListExpr))
    ):
        items = e.operands[1].items
        n_items = len(items)
        # x in y -> x == y[0] or ... or x == y[n]
        # x not in y -> x != y[0] and ... and x != y[n]
        # 16 is arbitrarily chosen to limit code size
        if 1 < n_items < 16:
            if e.operators[0] == "in":
                bin_op = "or"
                cmp_op = "=="
            else:
                bin_op = "and"
                cmp_op = "!="
            lhs = e.operands[0]
            mypy_file = builder.graph["builtins"].tree
            assert mypy_file is not None
            info = mypy_file.names["bool"].node
            assert isinstance(info, TypeInfo)
            bool_type = Instance(info, [])
            exprs = []
            for item in items:
                expr = ComparisonExpr([cmp_op], [lhs, item])
                builder.types[expr] = bool_type
                exprs.append(expr)

            or_expr: Expression = exprs.pop(0)
            for expr in exprs:
                or_expr = OpExpr(bin_op, or_expr, expr)
                builder.types[or_expr] = bool_type
            return builder.accept(or_expr)
        # x in [y]/(y) -> x == y
        # x not in [y]/(y) -> x != y
        elif n_items == 1:
            if e.operators[0] == "in":
                cmp_op = "=="
            else:
                cmp_op = "!="
            e.operators = [cmp_op]
            e.operands[1] = items[0]
        # x in []/() -> False
        # x not in []/() -> True
        elif n_items == 0:
            if e.operators[0] == "in":
                return builder.false()
            else:
                return builder.true()

    # x in {...}
    # x not in {...}
    if (
        first_op in ("in", "not in")
        and len(e.operators) == 1
        and isinstance(e.operands[1], SetExpr)
    ):
        set_literal = precompute_set_literal(builder, e.operands[1])
        if set_literal is not None:
            lhs = e.operands[0]
            result = builder.builder.primitive_op(
                set_in_op, [builder.accept(lhs), set_literal], e.line, bool_rprimitive
            )
            if first_op == "not in":
                return builder.unary_op(result, "not", e.line)
            return result

    if len(e.operators) == 1:
        # Special some common simple cases
        if first_op in ("is", "is not"):
            right_expr = e.operands[1]
            if isinstance(right_expr, NameExpr) and right_expr.fullname == "builtins.None":
                # Special case 'is None' / 'is not None'.
                return translate_is_none(builder, e.operands[0], negated=first_op != "is")
        left_expr = e.operands[0]
        if is_int_rprimitive(builder.node_type(left_expr)):
            right_expr = e.operands[1]
            if is_int_rprimitive(builder.node_type(right_expr)):
                if first_op in int_borrow_friendly_op:
                    borrow_left = is_borrow_friendly_expr(builder, right_expr)
                    left = builder.accept(left_expr, can_borrow=borrow_left)
                    right = builder.accept(right_expr, can_borrow=True)
                    return builder.binary_op(left, right, first_op, e.line)

    # TODO: Don't produce an expression when used in conditional context
    # All of the trickiness here is due to support for chained conditionals
    # (`e1 < e2 > e3`, etc). `e1 < e2 > e3` is approximately equivalent to
    # `e1 < e2 and e2 > e3` except that `e2` is only evaluated once.
    expr_type = builder.node_type(e)

    # go(i, prev) generates code for `ei opi e{i+1} op{i+1} ... en`,
    # assuming that prev contains the value of `ei`.
    def go(i: int, prev: Value) -> Value:
        if i == len(e.operators) - 1:
            return transform_basic_comparison(
                builder, e.operators[i], prev, builder.accept(e.operands[i + 1]), e.line
            )

        next = builder.accept(e.operands[i + 1])
        return builder.builder.shortcircuit_helper(
            "and",
            expr_type,
            lambda: transform_basic_comparison(builder, e.operators[i], prev, next, e.line),
            lambda: go(i + 1, next),
            e.line,
        )

    return go(0, builder.accept(e.operands[0]))


def translate_is_none(builder: IRBuilder, expr: Expression, negated: bool) -> Value:
    v = builder.accept(expr, can_borrow=True)
    return builder.binary_op(v, builder.none_object(), "is not" if negated else "is", expr.line)


def transform_basic_comparison(
    builder: IRBuilder, op: str, left: Value, right: Value, line: int
) -> Value:
    if is_fixed_width_rtype(left.type) and op in ComparisonOp.signed_ops:
        if right.type == left.type:
            if left.type.is_signed:
                op_id = ComparisonOp.signed_ops[op]
            else:
                op_id = ComparisonOp.unsigned_ops[op]
            return builder.builder.comparison_op(left, right, op_id, line)
        elif isinstance(right, Integer):
            if left.type.is_signed:
                op_id = ComparisonOp.signed_ops[op]
            else:
                op_id = ComparisonOp.unsigned_ops[op]
            return builder.builder.comparison_op(
                left, builder.coerce(right, left.type, line), op_id, line
            )
    elif (
        is_fixed_width_rtype(right.type)
        and op in ComparisonOp.signed_ops
        and isinstance(left, Integer)
    ):
        if right.type.is_signed:
            op_id = ComparisonOp.signed_ops[op]
        else:
            op_id = ComparisonOp.unsigned_ops[op]
        return builder.builder.comparison_op(
            builder.coerce(left, right.type, line), right, op_id, line
        )

    negate = False
    if op == "is not":
        op, negate = "is", True
    elif op == "not in":
        op, negate = "in", True

    target = builder.binary_op(left, right, op, line)

    if negate:
        target = builder.unary_op(target, "not", line)
    return target


def translate_printf_style_formatting(
    builder: IRBuilder, format_expr: StrExpr | BytesExpr, rhs: Expression
) -> Value | None:
    tokens = tokenizer_printf_style(format_expr.value)
    if tokens is not None:
        literals, format_ops = tokens

        exprs = []
        if isinstance(rhs, TupleExpr):
            exprs = rhs.items
        elif isinstance(rhs, Expression):
            exprs.append(rhs)

        if isinstance(format_expr, BytesExpr):
            substitutions = convert_format_expr_to_bytes(
                builder, format_ops, exprs, format_expr.line
            )
            if substitutions is not None:
                return join_formatted_bytes(builder, literals, substitutions, format_expr.line)
        else:
            substitutions = convert_format_expr_to_str(
                builder, format_ops, exprs, format_expr.line
            )
            if substitutions is not None:
                return join_formatted_strings(builder, literals, substitutions, format_expr.line)

    return None


# Literals


def transform_int_expr(builder: IRBuilder, expr: IntExpr) -> Value:
    return builder.builder.load_int(expr.value)


def transform_float_expr(builder: IRBuilder, expr: FloatExpr) -> Value:
    return builder.builder.load_float(expr.value)


def transform_complex_expr(builder: IRBuilder, expr: ComplexExpr) -> Value:
    return builder.builder.load_complex(expr.value)


def transform_str_expr(builder: IRBuilder, expr: StrExpr) -> Value:
    return builder.load_str(expr.value)


def transform_bytes_expr(builder: IRBuilder, expr: BytesExpr) -> Value:
    return builder.load_bytes_from_str_literal(expr.value)


def transform_ellipsis(builder: IRBuilder, o: EllipsisExpr) -> Value:
    return builder.add(LoadAddress(ellipsis_op.type, ellipsis_op.src, o.line))


# Display expressions


def transform_list_expr(builder: IRBuilder, expr: ListExpr) -> Value:
    return _visit_list_display(builder, expr.items, expr.line)


def _visit_list_display(builder: IRBuilder, items: list[Expression], line: int) -> Value:
    return _visit_display(
        builder, items, builder.new_list_op, list_append_op, list_extend_op, line, True
    )


def transform_tuple_expr(builder: IRBuilder, expr: TupleExpr) -> Value:
    if any(isinstance(item, StarExpr) for item in expr.items):
        # create a tuple of unknown length
        return _visit_tuple_display(builder, expr)

    # create a tuple of fixed length (RTuple)
    tuple_type = builder.node_type(expr)
    # When handling NamedTuple et. al we might not have proper type info,
    # so make some up if we need it.
    types = (
        tuple_type.types
        if isinstance(tuple_type, RTuple)
        else [object_rprimitive] * len(expr.items)
    )

    items = []
    for item_expr, item_type in zip(expr.items, types):
        reg = builder.accept(item_expr)
        items.append(builder.coerce(reg, item_type, item_expr.line))
    return builder.add(TupleSet(items, expr.line))


def _visit_tuple_display(builder: IRBuilder, expr: TupleExpr) -> Value:
    """Create a list, then turn it into a tuple."""
    val_as_list = _visit_list_display(builder, expr.items, expr.line)
    return builder.primitive_op(list_tuple_op, [val_as_list], expr.line)


def transform_dict_expr(builder: IRBuilder, expr: DictExpr) -> Value:
    """First accepts all keys and values, then makes a dict out of them."""
    key_value_pairs = []
    for key_expr, value_expr in expr.items:
        key = builder.accept(key_expr) if key_expr is not None else None
        value = builder.accept(value_expr)
        key_value_pairs.append((key, value))

    return builder.builder.make_dict(key_value_pairs, expr.line)


def transform_set_expr(builder: IRBuilder, expr: SetExpr) -> Value:
    return _visit_display(
        builder, expr.items, builder.new_set_op, set_add_op, set_update_op, expr.line, False
    )


def _visit_display(
    builder: IRBuilder,
    items: list[Expression],
    constructor_op: Callable[[list[Value], int], Value],
    append_op: PrimitiveDescription,
    extend_op: PrimitiveDescription,
    line: int,
    is_list: bool,
) -> Value:
    accepted_items = []
    for item in items:
        if isinstance(item, StarExpr):
            accepted_items.append((True, builder.accept(item.expr)))
        else:
            accepted_items.append((False, builder.accept(item)))

    result: Value | None = None
    initial_items = []
    for starred, value in accepted_items:
        if result is None and not starred and is_list:
            initial_items.append(value)
            continue

        if result is None:
            result = constructor_op(initial_items, line)

        builder.primitive_op(extend_op if starred else append_op, [result, value], line)

    if result is None:
        result = constructor_op(initial_items, line)

    return result


# Comprehensions


def transform_list_comprehension(builder: IRBuilder, o: ListComprehension) -> Value:
    return translate_list_comprehension(builder, o.generator)


def transform_set_comprehension(builder: IRBuilder, o: SetComprehension) -> Value:
    return translate_set_comprehension(builder, o.generator)


def transform_dictionary_comprehension(builder: IRBuilder, o: DictionaryComprehension) -> Value:
    if raise_error_if_contains_unreachable_names(builder, o):
        return builder.none()

    d = builder.maybe_spill(builder.call_c(dict_new_op, [], o.line))
    loop_params = list(zip(o.indices, o.sequences, o.condlists, o.is_async))

    def gen_inner_stmts() -> None:
        k = builder.accept(o.key)
        v = builder.accept(o.value)
        builder.primitive_op(dict_set_item_op, [builder.read(d), k, v], o.line)

    comprehension_helper(builder, loop_params, gen_inner_stmts, o.line)
    return builder.read(d)


# Misc


def transform_slice_expr(builder: IRBuilder, expr: SliceExpr) -> Value:
    def get_arg(arg: Expression | None) -> Value:
        if arg is None:
            return builder.none_object()
        else:
            return builder.accept(arg)

    args = [get_arg(expr.begin_index), get_arg(expr.end_index), get_arg(expr.stride)]
    return builder.primitive_op(new_slice_op, args, expr.line)


def transform_generator_expr(builder: IRBuilder, o: GeneratorExpr) -> Value:
    builder.warning("Treating generator comprehension as list", o.line)
    return builder.primitive_op(iter_op, [translate_list_comprehension(builder, o)], o.line)


def transform_assignment_expr(builder: IRBuilder, o: AssignmentExpr) -> Value:
    value = builder.accept(o.value)
    target = builder.get_assignment_target(o.target)
    builder.assign(target, value, o.line)
    return value


def transform_math_literal(builder: IRBuilder, fullname: str) -> Value | None:
    if fullname == "math.e":
        return builder.load_float(math.e)
    if fullname == "math.pi":
        return builder.load_float(math.pi)
    if fullname == "math.inf":
        return builder.load_float(math.inf)
    if fullname == "math.nan":
        return builder.load_float(math.nan)
    if fullname == "math.tau":
        return builder.load_float(math.tau)

    return None
