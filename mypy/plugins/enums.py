"""
This file contains a variety of plugins for refining how mypy infers types of
expressions involving Enums.

Currently, this file focuses on providing better inference for expressions like
'SomeEnum.FOO.name' and 'SomeEnum.FOO.value'. Note that the type of both expressions
will vary depending on exactly which instance of SomeEnum we're looking at.

Note that this file does *not* contain all special-cased logic related to enums:
we actually bake some of it directly in to the semantic analysis layer (see
semanal_enum.py).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar, cast

import mypy.plugin  # To avoid circular imports.
from mypy.nodes import TypeInfo
from mypy.subtypes import is_equivalent
from mypy.typeops import fixup_partial_type, make_simplified_union
from mypy.types import (
    CallableType,
    Instance,
    LiteralType,
    ProperType,
    Type,
    get_proper_type,
    is_named_instance,
)


def enum_name_callback(ctx: mypy.plugin.AttributeContext) -> Type:
    """This plugin refines the 'name' attribute in enums to act as if
    they were declared to be final.

    For example, the expression 'MyEnum.FOO.name' normally is inferred
    to be of type 'str'.

    This plugin will instead make the inferred type be a 'str' where the
    last known value is 'Literal["FOO"]'. This means it would be legal to
    use 'MyEnum.FOO.name' in contexts that expect a Literal type, just like
    any other Final variable or attribute.

    This plugin assumes that the provided context is an attribute access
    matching one of the strings found in 'ENUM_NAME_ACCESS'.
    """
    enum_field_name = _extract_underlying_field_name(ctx.type)
    if enum_field_name is None:
        return ctx.default_attr_type
    else:
        str_type = ctx.api.named_generic_type("builtins.str", [])
        literal_type = LiteralType(enum_field_name, fallback=str_type)
        return str_type.copy_modified(last_known_value=literal_type)


_T = TypeVar("_T")


def _first(it: Iterable[_T]) -> _T | None:
    """Return the first value from any iterable.

    Returns ``None`` if the iterable is empty.
    """
    for val in it:
        return val
    return None


def _infer_value_type_with_auto_fallback(
    ctx: mypy.plugin.AttributeContext, proper_type: ProperType | None
) -> Type | None:
    """Figure out the type of an enum value accounting for `auto()`.

    This method is a no-op for a `None` proper_type and also in the case where
    the type is not "enum.auto"
    """
    if proper_type is None:
        return None
    proper_type = get_proper_type(fixup_partial_type(proper_type))
    if not (isinstance(proper_type, Instance) and proper_type.type.fullname == "enum.auto"):
        if is_named_instance(proper_type, "enum.member") and proper_type.args:
            return proper_type.args[0]
        return proper_type
    assert isinstance(ctx.type, Instance), "An incorrect ctx.type was passed."
    info = ctx.type.type
    # Find the first _generate_next_value_ on the mro.  We need to know
    # if it is `Enum` because `Enum` types say that the return-value of
    # `_generate_next_value_` is `Any`.  In reality the default `auto()`
    # returns an `int` (presumably the `Any` in typeshed is to make it
    # easier to subclass and change the returned type).
    type_with_gnv = _first(ti for ti in info.mro if ti.names.get("_generate_next_value_"))
    if type_with_gnv is None:
        return ctx.default_attr_type

    stnode = type_with_gnv.names["_generate_next_value_"]

    # This should be a `CallableType`
    node_type = get_proper_type(stnode.type)
    if isinstance(node_type, CallableType):
        if type_with_gnv.fullname == "enum.Enum":
            int_type = ctx.api.named_generic_type("builtins.int", [])
            return int_type
        return get_proper_type(node_type.ret_type)
    return ctx.default_attr_type


def _implements_new(info: TypeInfo) -> bool:
    """Check whether __new__ comes from enum.Enum or was implemented in a
    subclass. In the latter case, we must infer Any as long as mypy can't infer
    the type of _value_ from assignments in __new__.
    """
    type_with_new = _first(
        ti
        for ti in info.mro
        if ti.names.get("__new__") and not ti.fullname.startswith("builtins.")
    )
    if type_with_new is None:
        return False
    return type_with_new.fullname not in ("enum.Enum", "enum.IntEnum", "enum.StrEnum")


def enum_member_callback(ctx: mypy.plugin.FunctionContext) -> Type:
    """By default `member(1)` will be inferred as `member[int]`,
    we want to improve the inference to be `Literal[1]` here."""
    if ctx.arg_types or ctx.arg_types[0]:
        arg = get_proper_type(ctx.arg_types[0][0])
        proper_return = get_proper_type(ctx.default_return_type)
        if (
            isinstance(arg, Instance)
            and arg.last_known_value
            and isinstance(proper_return, Instance)
            and len(proper_return.args) == 1
        ):
            return proper_return.copy_modified(args=[arg])
    return ctx.default_return_type


def enum_value_callback(ctx: mypy.plugin.AttributeContext) -> Type:
    """This plugin refines the 'value' attribute in enums to refer to
    the original underlying value. For example, suppose we have the
    following:

        class SomeEnum:
            FOO = A()
            BAR = B()

    By default, mypy will infer that 'SomeEnum.FOO.value' and
    'SomeEnum.BAR.value' both are of type 'Any'. This plugin refines
    this inference so that mypy understands the expressions are
    actually of types 'A' and 'B' respectively. This better reflects
    the actual runtime behavior.

    This plugin works simply by looking up the original value assigned
    to the enum. For example, when this plugin sees 'SomeEnum.BAR.value',
    it will look up whatever type 'BAR' had in the SomeEnum TypeInfo and
    use that as the inferred type of the overall expression.

    This plugin assumes that the provided context is an attribute access
    matching one of the strings found in 'ENUM_VALUE_ACCESS'.
    """
    enum_field_name = _extract_underlying_field_name(ctx.type)
    if enum_field_name is None:
        # We do not know the enum field name (perhaps it was passed to a
        # function and we only know that it _is_ a member).  All is not lost
        # however, if we can prove that the all of the enum members have the
        # same value-type, then it doesn't matter which member was passed in.
        # The value-type is still known.
        if isinstance(ctx.type, Instance):
            info = ctx.type.type

            # As long as mypy doesn't understand attribute creation in __new__,
            # there is no way to predict the value type if the enum class has a
            # custom implementation
            if _implements_new(info):
                return ctx.default_attr_type

            stnodes = (info.get(name) for name in info.names)

            # Enums _can_ have methods, instance attributes, and `nonmember`s.
            # Omit methods and attributes created by assigning to self.*
            # for our value inference.
            node_types = (
                get_proper_type(n.type) if n else None
                for n in stnodes
                if n is None or not n.implicit
            )
            proper_types = [
                _infer_value_type_with_auto_fallback(ctx, t)
                for t in node_types
                if t is None
                or (not isinstance(t, CallableType) and not is_named_instance(t, "enum.nonmember"))
            ]
            underlying_type = _first(proper_types)
            if underlying_type is None:
                return ctx.default_attr_type

            # At first we try to predict future `value` type if all other items
            # have the same type. For example, `int`.
            # If this is the case, we simply return this type.
            # See https://github.com/python/mypy/pull/9443
            all_same_value_type = all(
                proper_type is not None and proper_type == underlying_type
                for proper_type in proper_types
            )
            if all_same_value_type:
                if underlying_type is not None:
                    return underlying_type

            # But, after we started treating all `Enum` values as `Final`,
            # we start to infer types in
            # `item = 1` as `Literal[1]`, not just `int`.
            # So, for example types in this `Enum` will all be different:
            #
            #  class Ordering(IntEnum):
            #      one = 1
            #      two = 2
            #      three = 3
            #
            # We will infer three `Literal` types here.
            # They are not the same, but they are equivalent.
            # So, we unify them to make sure `.value` prediction still works.
            # Result will be `Literal[1] | Literal[2] | Literal[3]` for this case.
            all_equivalent_types = all(
                proper_type is not None and is_equivalent(proper_type, underlying_type)
                for proper_type in proper_types
            )
            if all_equivalent_types:
                return make_simplified_union(cast(Sequence[Type], proper_types))
        return ctx.default_attr_type

    assert isinstance(ctx.type, Instance)
    info = ctx.type.type

    # As long as mypy doesn't understand attribute creation in __new__,
    # there is no way to predict the value type if the enum class has a
    # custom implementation
    if _implements_new(info):
        return ctx.default_attr_type

    stnode = info.get(enum_field_name)
    if stnode is None:
        return ctx.default_attr_type

    underlying_type = _infer_value_type_with_auto_fallback(ctx, get_proper_type(stnode.type))
    if underlying_type is None:
        return ctx.default_attr_type

    return underlying_type


def _extract_underlying_field_name(typ: Type) -> str | None:
    """If the given type corresponds to some Enum instance, returns the
    original name of that enum. For example, if we receive in the type
    corresponding to 'SomeEnum.FOO', we return the string "SomeEnum.Foo".

    This helper takes advantage of the fact that Enum instances are valid
    to use inside Literal[...] types. An expression like 'SomeEnum.FOO' is
    actually represented by an Instance type with a Literal enum fallback.

    We can examine this Literal fallback to retrieve the string.
    """
    typ = get_proper_type(typ)
    if not isinstance(typ, Instance):
        return None

    if not typ.type.is_enum:
        return None

    underlying_literal = typ.last_known_value
    if underlying_literal is None:
        return None

    # The checks above have verified this LiteralType is representing an enum value,
    # which means the 'value' field is guaranteed to be the name of the enum field
    # as a string.
    assert isinstance(underlying_literal.value, str)
    return underlying_literal.value
