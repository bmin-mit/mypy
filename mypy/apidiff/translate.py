"""Translate types from the old build so that they refer to the new build's classes.

mypy's subtype checks identify classes by TypeInfo identity (see mypy.maptype), so a type
from one build can't be compared with a type from another build directly. We rewrite
every Instance in an old type to point to the TypeInfo with the same fullname in the new
build. Classes that no longer exist are recorded as unresolved and replaced with Any, and
the caller falls back to comparing formatted type names.
"""

from __future__ import annotations

from collections.abc import Sequence

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import MypyFile, TypeAlias, TypeInfo
from mypy.type_visitor import TypeTranslator
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    LiteralType,
    Parameters,
    ParamSpecType,
    ProperType,
    Type,
    TypeAliasType,
    TypeOfAny,
    TypeVarLikeType,
    TypeVarTupleType,
    TypeVarType,
    get_proper_type,
)


class TranslationResult:
    def __init__(self, typ: Type, unresolved: set[str], relaxed: bool) -> None:
        # The translated type, referring only to new-build TypeInfos
        self.type = typ
        # Fullnames of classes that don't exist in the new build
        self.unresolved = unresolved
        # True if some generic arity mismatch forced us to fill type arguments with Any
        self.relaxed = relaxed


class OldToNewTranslator(TypeTranslator):
    def __init__(self, new_modules: dict[str, MypyFile]) -> None:
        super().__init__()
        self.new_modules = new_modules
        self.info_cache: dict[str, TypeInfo | None] = {}
        self.alias_cache: dict[str, TypeAlias | None] = {}
        self.unresolved: set[str] = set()
        self.relaxed = False
        self.expanding: list[str] = []

    def translate(self, typ: Type) -> TranslationResult:
        self.unresolved = set()
        self.relaxed = False
        result = typ.accept(self)
        return TranslationResult(result, self.unresolved, self.relaxed)

    def translate_info(self, info: TypeInfo) -> TypeInfo | None:
        return self.lookup_info(info.fullname)

    def lookup_info(self, fullname: str) -> TypeInfo | None:
        if fullname not in self.info_cache:
            sym = lookup_fully_qualified(fullname, self.new_modules)
            node = sym.node if sym is not None else None
            self.info_cache[fullname] = node if isinstance(node, TypeInfo) else None
        return self.info_cache[fullname]

    def lookup_alias(self, fullname: str) -> TypeAlias | None:
        if fullname not in self.alias_cache:
            sym = lookup_fully_qualified(fullname, self.new_modules)
            node = sym.node if sym is not None else None
            self.alias_cache[fullname] = node if isinstance(node, TypeAlias) else None
        return self.alias_cache[fullname]

    def visit_instance(self, t: Instance, /) -> Type:
        info = self.lookup_info(t.type.fullname)
        if info is None:
            self.unresolved.add(t.type.fullname)
            return AnyType(TypeOfAny.special_form)
        args = self.translate_type_tuple(t.args)
        if len(args) != len(info.type_vars) and not info.has_type_var_tuple_type:
            # The class gained or lost type parameters; this is reported separately.
            self.relaxed = True
            args = tuple(AnyType(TypeOfAny.from_omitted_generics) for _ in info.type_vars)
        last_known_value = None
        if t.last_known_value is not None:
            lkv = get_proper_type(t.last_known_value.accept(self))
            if isinstance(lkv, LiteralType):
                last_known_value = lkv
        return Instance(
            info, args, line=t.line, column=t.column, last_known_value=last_known_value
        )

    def visit_callable_type(self, t: CallableType, /) -> Type:
        result = get_proper_type(super().visit_callable_type(t))
        assert isinstance(result, CallableType)
        fallback = get_proper_type(t.fallback.accept(self))
        if isinstance(fallback, Instance):
            result = result.copy_modified(fallback=fallback)
        return result

    def visit_type_var(self, t: TypeVarType, /) -> Type:
        return t.copy_modified(
            values=self.translate_type_list(t.values),
            upper_bound=t.upper_bound.accept(self),
            default=t.default.accept(self),
        )

    def visit_param_spec(self, t: ParamSpecType, /) -> Type:
        prefix = t.prefix.accept(self)
        assert isinstance(prefix, Parameters)
        return ParamSpecType(
            t.name,
            t.fullname,
            t.id,
            t.flavor,
            t.upper_bound.accept(self),
            t.default.accept(self),
            line=t.line,
            column=t.column,
            prefix=prefix,
        )

    def visit_type_var_tuple(self, t: TypeVarTupleType, /) -> Type:
        fallback = get_proper_type(t.tuple_fallback.accept(self))
        if not isinstance(fallback, Instance):
            return t
        return TypeVarTupleType(
            t.name,
            t.fullname,
            t.id,
            t.upper_bound.accept(self),
            fallback,
            t.default.accept(self),
            line=t.line,
            column=t.column,
            min_len=t.min_len,
        )

    def translate_variables(
        self, variables: Sequence[TypeVarLikeType]
    ) -> Sequence[TypeVarLikeType]:
        result = []
        for v in variables:
            translated = v.accept(self)
            assert isinstance(translated, TypeVarLikeType)
            result.append(translated)
        return result

    def visit_type_alias_type(self, t: TypeAliasType, /) -> Type:
        # Expand using the *old* definition: the alias target may have changed.
        assert t.alias is not None
        fullname = t.alias.fullname
        if fullname in self.expanding:
            # A recursive alias. Refer to the new alias of the same name, if any.
            new_alias = self.lookup_alias(fullname)
            if new_alias is None:
                self.unresolved.add(fullname)
                return AnyType(TypeOfAny.special_form)
            return TypeAliasType(new_alias, list(self.translate_type_tuple(tuple(t.args))))
        self.expanding.append(fullname)
        try:
            return get_proper_type(t).accept(self)
        finally:
            self.expanding.pop()


def translate_proper(translator: OldToNewTranslator, typ: Type) -> ProperType:
    return get_proper_type(translator.translate(typ).type)
