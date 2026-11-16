"""Compare the public API of two built versions and classify each difference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mypy.apidiff.build import BuiltVersion
from mypy.apidiff.surface import (
    ALIAS,
    ATTRIBUTE,
    CLASS,
    MODULE,
    TYPEVAR,
    ApiSymbol,
    Member,
    class_members,
    collect_module_symbols,
    is_private_module,
    member_location,
)
from mypy.apidiff.translate import OldToNewTranslator
from mypy.expandtype import expand_type, expand_type_by_instance
from mypy.maptype import map_instance_to_supertype
from mypy.messages import format_type_bare, pretty_callable
from mypy.nodes import (
    CONTRAVARIANT,
    COVARIANT,
    INVARIANT,
    ArgKind,
    Decorator,
    FuncBase,
    FuncDef,
    MypyFile,
    OverloadedFuncDef,
    ParamSpecExpr,
    SymbolNode,
    TypeAlias,
    TypeInfo,
    TypeVarExpr,
    TypeVarTupleExpr,
    Var,
)
from mypy.options import Options
from mypy.state import state
from mypy.subtypes import is_proper_subtype, is_same_type, is_subtype
from mypy.typeops import bind_self, function_type, type_object_type
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    Overloaded,
    TupleType,
    Type,
    TypeOfAny,
    TypeVarId,
    TypeVarLikeType,
    TypeVarType,
    get_proper_type,
)
from mypy.typevars import fill_typevars

BREAKING: Final = "breaking"
WARNING: Final = "warning"
INFO: Final = "info"

SEVERITIES: Final = (BREAKING, WARNING, INFO)

# Every change code with its severity and a short description (used in docs and tests).
CODES: Final = {
    # Removals and kind changes
    "module-removed": (BREAKING, "A public module was removed"),
    "symbol-removed": (BREAKING, "A public name, class member or TypedDict key was removed"),
    "kind-changed": (BREAKING, "A name changed kind, e.g. from a class to a function"),
    # Functions and methods
    "param-removed": (BREAKING, "A parameter that callers could pass was removed"),
    "param-renamed": (BREAKING, "A parameter that can be passed by keyword was renamed"),
    "param-reordered": (BREAKING, "A positional parameter moved to another position"),
    "param-kind-changed": (
        BREAKING,
        "A parameter can no longer be passed positionally, or by keyword",
    ),
    "param-default-removed": (BREAKING, "An optional parameter became required"),
    "param-added-required": (BREAKING, "A new required parameter was added"),
    "param-type-narrowed": (BREAKING, "A parameter accepts fewer types than before"),
    "star-args-removed": (BREAKING, "*args or **kwargs is no longer accepted"),
    "return-widened": (BREAKING, "A return type now includes more types than before"),
    "signature-incompatible": (
        BREAKING,
        "The new signature can't be used where the old one was expected",
    ),
    "method-kind-changed": (
        BREAKING,
        "A member changed between instance method, class/static method and property",
    ),
    "overload-variant-removed": (BREAKING, "An overload variant is no longer supported"),
    # Attributes
    "attr-type-changed": (BREAKING, "The type of a mutable attribute changed"),
    "attr-widened": (BREAKING, "The type of a read-only attribute was widened"),
    "attr-became-readonly": (BREAKING, "A writable attribute became read-only"),
    "attr-became-classvar": (BREAKING, "An instance attribute became a ClassVar"),
    "typeddict-key-required": (BREAKING, "A TypedDict key became required, or was added"),
    # Classes
    "class-base-removed": (BREAKING, "A class no longer inherits from a public base class"),
    "class-became-final": (BREAKING, "A class became @final"),
    "method-became-final": (BREAKING, "A method became @final"),
    "abstract-method-added": (BREAKING, "A class has a new abstract member"),
    "metaclass-changed": (BREAKING, "The metaclass changed to an unrelated class"),
    "protocol-member-added": (BREAKING, "A protocol has a new member"),
    "class-no-longer-protocol": (BREAKING, "A protocol became a regular (nominal) class"),
    # Generics
    "typeparam-removed": (BREAKING, "A type parameter was removed"),
    "typeparam-reordered": (BREAKING, "Type parameters were reordered"),
    "typeparam-bound-narrowed": (BREAKING, "A type parameter's bound or constraints narrowed"),
    "typeparam-variance-changed": (BREAKING, "A type parameter's variance changed incompatibly"),
    "typeparam-added": (WARNING, "A type parameter was added"),
    "typeparam-bound-widened": (INFO, "A type parameter's bound or constraints widened"),
    # Warnings
    "alias-changed": (WARNING, "The target of a type alias changed"),
    "any-changed": (WARNING, "A type changed in a way that is compatible only because of Any"),
    "type-unresolved": (WARNING, "A type refers to a class that no longer exists"),
    "missing-dependency": (WARNING, "An import could not be resolved; its types are Any"),
    "unused-allowlist": (WARNING, "An allowlist entry didn't match any change"),
    # Informational
    "symbol-added": (INFO, "A public name or member was added"),
    "param-added-optional": (INFO, "A new optional parameter was added"),
    "param-widened": (INFO, "A parameter accepts more types than before"),
    "return-narrowed": (INFO, "A return type became more specific"),
}

# Results of comparing two types
SAME: Final = "same"
OK: Final = "ok"
ANY: Final = "any"
BAD: Final = "bad"
UNRESOLVED: Final = "unresolved"

# Kinds of views (see View) on top of the symbol kinds from surface.py
CALLABLE: Final = "callable"
OTHER: Final = "other"

# Method kinds
INSTANCE_METHOD: Final = "instance method"
CLASS_METHOD: Final = "class method"
STATIC_METHOD: Final = "static method"
PROPERTY: Final = "property"


@dataclass(frozen=True)
class Location:
    path: str
    line: int
    signature: str | None = None


@dataclass
class Change:
    code: str
    severity: str
    symbol: str
    message: str
    old: Location | None
    new: Location | None


@dataclass
class Finding:
    """A change before it gets a location attached."""

    code: str
    message: str

    @property
    def severity(self) -> str:
        return CODES[self.code][0]


@dataclass
class View:
    """What a caller sees when using a symbol: its kind and its type."""

    kind: str
    node: SymbolNode
    # Callables: the type as seen through an instance (self already bound).
    # Attributes and properties: the value type. Aliases: the target.
    type: Type | None = None
    info: TypeInfo | None = None
    readonly: bool = False
    classvar: bool = False
    final: bool = False
    method_kind: str | None = None


def quote(s: str) -> str:
    return f'"{s}"'


def describe_kind(view: View) -> str:
    if view.method_kind is not None:
        return view.method_kind
    return {
        CLASS: "class",
        CALLABLE: "function",
        ATTRIBUTE: "attribute",
        ALIAS: "type alias",
        TYPEVAR: "type variable",
        MODULE: "module",
    }.get(view.kind, "name")


def param_desc(name: str | None, index: int) -> str:
    if name is None:
        return f"positional parameter {index + 1}"
    return f"parameter {quote(name)}"


def is_positional(kind: ArgKind) -> bool:
    return kind == ArgKind.ARG_POS or kind == ArgKind.ARG_OPT


def find_positional(c: CallableType, index: int) -> int | None:
    if index < len(c.arg_kinds) and is_positional(c.arg_kinds[index]):
        return index
    return None


def find_by_name(c: CallableType, name: str | None) -> int | None:
    if name is None:
        return None
    for i, (n, k) in enumerate(zip(c.arg_names, c.arg_kinds)):
        if n == name and not k.is_star():
            return i
    return None


def find_kind(c: CallableType, kind: ArgKind) -> int | None:
    for i, k in enumerate(c.arg_kinds):
        if k == kind:
            return i
    return None


class ApiComparer:
    def __init__(self, old: BuiltVersion, new: BuiltVersion) -> None:
        self.old = old
        self.new = new
        self.translator = OldToNewTranslator(new.modules)
        self.changes: list[Change] = []
        self.compared_classes: set[tuple[int, int]] = set()
        self.symbols_checked = 0
        self.packages = set(old.packages) | set(new.packages)
        # Maps old type variable ids to the new type variables with the same name, so that
        # e.g. a changed bound doesn't make every use of the variable look different.
        self.tv_map: dict[TypeVarId, Type] = {}

    # Entry point

    def run(self) -> list[Change]:
        old_symbols = collect_module_symbols(self.old)
        new_symbols = collect_module_symbols(self.new)

        removed_modules = set()
        for fullname in sorted(old_symbols):
            old_sym = old_symbols[fullname]
            if old_sym.module in removed_modules:
                continue
            new_sym = new_symbols.get(fullname)
            if new_sym is None:
                if old_sym.kind == MODULE:
                    removed_modules.add(fullname)
                    self.add(
                        "module-removed",
                        fullname,
                        f"Module {quote(fullname)} was removed",
                        self.old_loc(old_sym),
                        None,
                    )
                else:
                    self.report_removed_name(old_sym)
                continue
            self.symbols_checked += 1
            self.compare_symbols(old_sym, new_sym)

        added_modules = set()
        for fullname in sorted(new_symbols):
            new_sym = new_symbols[fullname]
            if fullname in old_symbols or new_sym.module in added_modules:
                continue
            if new_sym.kind == MODULE:
                added_modules.add(fullname)
            self.add(
                "symbol-added",
                fullname,
                f"{describe_kind(self.view(new_sym.node, None, self.new)).capitalize()} "
                f"{quote(fullname)} was added",
                None,
                self.new_loc(new_sym),
            )

        self.report_missing_imports()
        return self.changes

    # Bookkeeping

    def add(
        self, code: str, symbol: str, message: str, old: Location | None, new: Location | None
    ) -> None:
        self.changes.append(Change(code, CODES[code][0], symbol, message, old, new))

    def add_findings(
        self, findings: list[Finding], symbol: str, old: Location | None, new: Location | None
    ) -> None:
        for f in findings:
            self.add(f.code, symbol, f.message, old, new)

    def old_loc(self, sym: ApiSymbol, view: View | None = None) -> Location:
        if view is None:
            view = self.view(sym.node, None, self.old)
        return Location(sym.path, sym.line, self.signature(view, self.old.options))

    def new_loc(self, sym: ApiSymbol, view: View | None = None) -> Location:
        if view is None:
            view = self.view(sym.node, None, self.new)
        return Location(sym.path, sym.line, self.signature(view, self.new.options))

    def member_loc(
        self, version: BuiltVersion, member: Member, module: str, view: View
    ) -> Location:
        path, line = member_location(version, member, module)
        return Location(path, line, self.signature(view, version.options))

    def report_removed_name(self, old_sym: ApiSymbol) -> None:
        module = self.new.modules.get(old_sym.module)
        name = old_sym.fullname.rsplit(".", 1)[1]
        if module is not None and name in module.names:
            message = f"{quote(old_sym.fullname)} is no longer exported"
        else:
            message = f"{quote(old_sym.fullname)} was removed"
        self.add("symbol-removed", old_sym.fullname, message, self.old_loc(old_sym), None)

    def report_missing_imports(self) -> None:
        reported = set()
        for version in (self.old, self.new):
            for module in sorted(version.missing_imports):
                if module in reported or module.split(".")[0] in self.packages:
                    continue
                reported.add(module)
                self.add(
                    "missing-dependency",
                    module,
                    f"Cannot resolve import {quote(module)}; types from it are treated as Any",
                    None,
                    None,
                )

    # Type helpers

    def fmt(self, typ: Type | None, options: Options) -> str:
        if typ is None:
            return "Any"
        return format_type_bare(typ, options)

    def fmt_old(self, typ: Type | None) -> str:
        return self.fmt(typ, self.old.options)

    def fmt_new(self, typ: Type | None) -> str:
        return self.fmt(typ, self.new.options)

    def signature(self, view: View, options: Options) -> str | None:
        typ = get_proper_type(view.type)
        if view.kind == CALLABLE and isinstance(typ, CallableType):
            return pretty_callable(typ, options, skip_self=True)
        if view.kind == CALLABLE and isinstance(typ, Overloaded):
            return "; ".join(pretty_callable(t, options, skip_self=True) for t in typ.items)
        if view.kind in (ATTRIBUTE, ALIAS) and typ is not None:
            return format_type_bare(typ, options)
        return None

    def check(self, old: Type | None, new: Type | None, variance: int) -> str:
        """Compare an old-build type with a new-build type.

        variance is COVARIANT if new must be a subtype of old (return values, read-only
        attributes), CONTRAVARIANT if old must be a subtype of new (parameters), and
        INVARIANT for both (mutable attributes).
        """
        if old is None:
            old = AnyType(TypeOfAny.unannotated)
        if new is None:
            new = AnyType(TypeOfAny.unannotated)
        result = self.translator.translate(self.unify_typevars(old))
        translated = result.type
        if result.unresolved:
            return SAME if self.fmt_old(old) == self.fmt_new(new) else UNRESOLVED
        if variance == COVARIANT:
            pairs = [(new, translated)]
        elif variance == CONTRAVARIANT:
            pairs = [(translated, new)]
        else:
            pairs = [(new, translated), (translated, new)]
        with state.strict_optional_set(True):
            if not all(is_subtype(left, right) for left, right in pairs):
                return BAD
            if all(is_proper_subtype(left, right) for left, right in pairs):
                return SAME if is_same_type(translated, new) else OK
        return OK if result.relaxed else ANY

    def translate(self, typ: Type) -> Type:
        return self.translator.translate(self.unify_typevars(typ)).type

    def unify_typevars(self, typ: Type) -> Type:
        return expand_type(typ, self.tv_map) if self.tv_map else typ

    def push_typevars(
        self, old_vars: list[TypeVarLikeType], new_vars: list[TypeVarLikeType]
    ) -> dict[TypeVarId, Type]:
        """Map old type variables to new ones by name. Returns the previous mapping."""
        saved = self.tv_map
        new_by_name = {v.name: v for v in new_vars}
        mapping = dict(saved)
        for old_var in old_vars:
            new_var = new_by_name.get(old_var.name)
            if new_var is not None and type(new_var) is type(old_var):
                mapping[old_var.id] = new_var
        self.tv_map = mapping
        return saved

    def specialize(self, typ: Type, owner: TypeInfo | None, defining: TypeInfo | None) -> Type:
        """Express a member type inherited from a generic base in terms of the owner."""
        if owner is None or defining is None or owner is defining or not defining.type_vars:
            return typ
        instance = fill_typevars(owner)
        if isinstance(instance, TupleType):
            instance = instance.partial_fallback
        return expand_type_by_instance(typ, map_instance_to_supertype(instance, defining))

    def verb(self, old: Type | None, new: Type | None, *, wider: str, narrower: str) -> str:
        """Describe an incompatible change: "widened"/"narrowed" if the types are related."""
        if old is None or new is None:
            return "changed"
        translated = self.translate(old)
        with state.strict_optional_set(True):
            if is_subtype(translated, new):
                return wider
            if is_subtype(new, translated):
                return narrower
        return "changed"

    # Building views

    def function_fallback(self, version: BuiltVersion) -> Instance:
        sym = version.modules["builtins"].names["function"]
        assert isinstance(sym.node, TypeInfo)
        return Instance(sym.node, [])

    def callable_of(self, node: FuncBase, version: BuiltVersion) -> FunctionLike:
        return function_type(node, self.function_fallback(version))

    def view(
        self,
        node: SymbolNode,
        owner: TypeInfo | None,
        version: BuiltVersion,
        defining: TypeInfo | None = None,
    ) -> View:
        """Describe what a caller sees when accessing node (through owner, if a member).

        defining is the class in owner's MRO that defines node.
        """
        view = self.raw_view(node, owner, version)
        if view.type is not None and view.kind in (CALLABLE, ATTRIBUTE):
            view.type = self.specialize(view.type, owner, defining)
        if view.kind == CALLABLE and owner is not None and view.method_kind != STATIC_METHOD:
            callable_type = get_proper_type(view.type)
            assert isinstance(callable_type, FunctionLike)
            view.type = self.bind(callable_type, owner, view.method_kind == CLASS_METHOD)
        return view

    def raw_view(self, node: SymbolNode, owner: TypeInfo | None, version: BuiltVersion) -> View:
        """Like view(), but without mapping inherited members or binding self."""
        if isinstance(node, MypyFile):
            return View(MODULE, node)
        if isinstance(node, TypeInfo):
            return View(CLASS, node, info=node)
        if isinstance(node, TypeAlias):
            target = get_proper_type(node.target)
            if node.no_args and isinstance(target, Instance):
                return View(CLASS, node, info=target.type)
            return View(ALIAS, node, type=node.target)
        if isinstance(node, (TypeVarExpr, ParamSpecExpr, TypeVarTupleExpr)):
            return View(TYPEVAR, node)
        if isinstance(node, Var):
            return self.var_view(node, owner)
        if isinstance(node, FuncDef):
            if node.is_property:
                ret = self.getter_type(node, version)
                return View(
                    ATTRIBUTE,
                    node,
                    ret,
                    readonly=True,
                    final=node.is_final,
                    method_kind=PROPERTY if owner else None,
                )
            return self.callable_view(
                node,
                self.callable_of(node, version),
                owner,
                node.is_static,
                node.is_class,
                node.is_final,
            )
        if isinstance(node, OverloadedFuncDef):
            first = node.items[0] if node.items else None
            if isinstance(first, Decorator) and first.var.is_property:
                ret = self.getter_type(first.func, version)
                return View(
                    ATTRIBUTE,
                    node,
                    ret,
                    readonly=not first.var.is_settable_property,
                    final=first.func.is_final or node.is_final,
                    method_kind=PROPERTY if owner else None,
                )
            typ = node.type if isinstance(node.type, FunctionLike) else None
            if typ is None:
                typ = self.callable_of(node, version)
            return self.callable_view(
                node, typ, owner, node.is_static, node.is_class, node.is_final
            )
        if isinstance(node, Decorator):
            func = node.func
            if node.var.is_property:
                ret = self.getter_type(func, version)
                return View(
                    ATTRIBUTE,
                    node,
                    ret,
                    readonly=not node.var.is_settable_property,
                    final=func.is_final,
                    method_kind=PROPERTY if owner else None,
                )
            typ = node.var.type if node.var.type is not None else self.callable_of(func, version)
            proper = get_proper_type(typ)
            if isinstance(proper, FunctionLike):
                return self.callable_view(
                    node,
                    proper,
                    owner,
                    func.is_static or node.var.is_staticmethod,
                    func.is_class or node.var.is_classmethod,
                    func.is_final,
                )
            # A decorator that returns some non-callable object (e.g. a descriptor)
            return View(ATTRIBUTE, node, typ, readonly=True, final=func.is_final)
        return View(OTHER, node)

    def getter_type(self, func: FuncDef, version: BuiltVersion) -> Type:
        typ = self.callable_of(func, version)
        if isinstance(typ, CallableType):
            return typ.ret_type
        return AnyType(TypeOfAny.special_form)

    def var_view(self, var: Var, owner: TypeInfo | None) -> View:
        typ = var.type if var.type is not None else AnyType(TypeOfAny.unannotated)
        readonly = var.is_final or (var.is_property and not var.is_settable_property)
        if owner is not None:
            dataclass_info = owner.metadata.get("dataclass", {})
            if dataclass_info.get("frozen") or owner.is_named_tuple:
                readonly = True
        return View(
            ATTRIBUTE,
            var,
            typ,
            readonly=readonly,
            classvar=var.is_classvar,
            final=var.is_final,
            method_kind=PROPERTY if owner is not None and var.is_property else None,
        )

    def callable_view(
        self,
        node: SymbolNode,
        typ: FunctionLike,
        owner: TypeInfo | None,
        is_static: bool,
        is_class: bool,
        is_final: bool,
    ) -> View:
        if owner is None:
            return View(CALLABLE, node, typ, final=is_final)
        if node.name == "__new__":
            is_class = True
        if is_static:
            kind = STATIC_METHOD
        else:
            kind = CLASS_METHOD if is_class else INSTANCE_METHOD
        return View(CALLABLE, node, typ, final=is_final, method_kind=kind)

    def bind(self, typ: FunctionLike, owner: TypeInfo, is_class: bool) -> FunctionLike:
        if any(not item.arg_types or item.arg_kinds[0].is_star() for item in typ.items):
            # No explicit self/cls (e.g. "def f(*args)"); compare the unbound signature.
            return typ
        try:
            return bind_self(typ, fill_typevars(owner), is_classmethod=is_class)
        except Exception:
            return typ

    # Symbol-level comparison

    def compare_symbols(self, old_sym: ApiSymbol, new_sym: ApiSymbol) -> None:
        if old_sym.kind == MODULE and new_sym.kind == MODULE:
            return
        ov = self.view(old_sym.node, None, self.old)
        nv = self.view(new_sym.node, None, self.new)
        old_loc = self.old_loc(old_sym, ov)
        new_loc = self.new_loc(new_sym, nv)
        if ov.kind == CLASS and nv.kind == CLASS:
            assert ov.info is not None and nv.info is not None
            if (
                ov.info.fullname != nv.info.fullname
                and not self.is_package_class(ov.info)
                and not self.is_package_class(nv.info)
            ):
                # An alias such as "Number = int" now points to a different external class.
                self.add(
                    "alias-changed",
                    old_sym.fullname,
                    f"{quote(old_sym.fullname)} changed from {quote(ov.info.fullname)} to "
                    f"{quote(nv.info.fullname)}",
                    old_loc,
                    new_loc,
                )
                return
            self.compare_classes(
                old_sym.fullname, ov.info, nv.info, old_sym.module, old_loc, new_loc
            )
            return
        findings = self.compare_views(old_sym.fullname, ov, nv, in_class=False)
        self.add_findings(findings, old_sym.fullname, old_loc, new_loc)

    def compare_views(self, fullname: str, ov: View, nv: View, in_class: bool) -> list[Finding]:
        name = quote(fullname)
        old_callable = ov.kind == CALLABLE or (
            ov.kind == ATTRIBUTE
            and not in_class
            and isinstance(get_proper_type(ov.type), FunctionLike)
        )
        new_callable = nv.kind == CALLABLE or (
            nv.kind == ATTRIBUTE
            and not in_class
            and isinstance(get_proper_type(nv.type), FunctionLike)
        )
        if ov.kind == CALLABLE and nv.kind == CALLABLE and in_class:
            findings = []
            if (ov.method_kind == INSTANCE_METHOD) != (nv.method_kind == INSTANCE_METHOD):
                findings.append(
                    Finding(
                        "method-kind-changed",
                        f"{name} changed from {article(ov.method_kind)} to "
                        f"{article(nv.method_kind)}",
                    )
                )
            return findings + self.compare_callables(fullname, ov.type, nv.type)
        if old_callable and new_callable:
            return self.compare_callables(fullname, ov.type, nv.type)
        if ov.kind == ATTRIBUTE and nv.kind == ATTRIBUTE:
            return self.compare_attributes(fullname, ov, nv, in_class)
        if (
            in_class
            and {ov.kind, nv.kind} == {CALLABLE, ATTRIBUTE}
            and PROPERTY in (ov.method_kind, nv.method_kind)
        ):
            return [
                Finding(
                    "method-kind-changed",
                    f"{name} changed from {article(describe_kind(ov))} to "
                    f"{article(describe_kind(nv))}",
                )
            ]
        if ov.kind == ALIAS and nv.kind == ALIAS:
            result = self.check(ov.type, nv.type, INVARIANT)
            if result in (SAME, OK, ANY):
                return []
            return [
                Finding(
                    "alias-changed",
                    f"Type alias {name} changed from {quote(self.fmt_old(ov.type))} "
                    f"to {quote(self.fmt_new(nv.type))}",
                )
            ]
        if ov.kind == nv.kind and ov.kind in (TYPEVAR, OTHER):
            return []
        return [
            Finding(
                "kind-changed",
                f"{name} changed from {article(describe_kind(ov))} to "
                f"{article(describe_kind(nv))}",
            )
        ]

    # Attributes

    def compare_attributes(
        self, fullname: str, ov: View, nv: View, in_class: bool
    ) -> list[Finding]:
        name = quote(fullname)
        findings = []
        if in_class and not ov.classvar and nv.classvar:
            findings.append(
                Finding(
                    "attr-became-classvar",
                    f"{name} became a ClassVar; it can no longer be set on instances",
                )
            )
        if not ov.readonly and nv.readonly:
            findings.append(Finding("attr-became-readonly", f"{name} is no longer writable"))
        readonly = ov.readonly or nv.readonly
        result = self.check(ov.type, nv.type, COVARIANT if readonly else INVARIANT)
        old_t = quote(self.fmt_old(ov.type))
        new_t = quote(self.fmt_new(nv.type))
        if result == BAD:
            if readonly:
                verb = self.verb(ov.type, nv.type, wider="widened", narrower="narrowed")
                findings.append(
                    Finding("attr-widened", f"Type of {name} {verb} from {old_t} to {new_t}")
                )
            else:
                findings.append(
                    Finding(
                        "attr-type-changed",
                        f"Type of mutable attribute {name} changed from {old_t} to {new_t}",
                    )
                )
        else:
            findings.extend(self.soft_findings(result, f"Type of {name}", old_t, new_t))
        return findings

    def soft_findings(self, result: str, what: str, old_t: str, new_t: str) -> list[Finding]:
        if result == ANY:
            return [
                Finding(
                    "any-changed",
                    f"{what} changed from {old_t} to {new_t}, compatible only because of Any",
                )
            ]
        if result == UNRESOLVED:
            return [
                Finding(
                    "type-unresolved",
                    f"{what} changed from {old_t} to {new_t}, which refers to a class "
                    "that no longer exists",
                )
            ]
        return []

    # Callables

    def compare_callables(
        self, fullname: str, old_t: Type | None, new_t: Type | None
    ) -> list[Finding]:
        old_p = get_proper_type(old_t)
        new_p = get_proper_type(new_t)
        if not isinstance(old_p, FunctionLike) or not isinstance(new_p, FunctionLike):
            return []
        old_items = old_p.items
        new_items = new_p.items
        if len(old_items) == 1 and len(new_items) == 1:
            return self.compare_signatures(fullname, old_items[0], new_items[0])

        findings: list[Finding] = []
        for old_item in old_items:
            translated = get_proper_type(self.translate(old_item))
            with state.strict_optional_set(True):
                covered = is_subtype(new_p, translated)
            if covered:
                continue
            closest = self.closest_item(old_item, new_items)
            details = [
                f
                for f in self.compare_signatures(fullname, old_item, closest)
                if f.severity == BREAKING
            ]
            if len(old_items) == 1:
                # The old function wasn't overloaded: explain against the closest variant.
                findings.extend(details or [self.incompatible(fullname, old_item, new_p)])
                continue
            old_sig = pretty_callable(old_item, self.old.options, skip_self=True)
            new_sig = pretty_callable(closest, self.new.options, skip_self=True)
            message = (
                f"Overload variant {quote(old_sig)} of {quote(fullname)} is no longer supported"
            )
            if len(new_items) > 1:
                message += f"; closest new variant is {quote(new_sig)}"
            if details:
                message += f": {details[0].message}"
            findings.append(Finding("overload-variant-removed", message))
        return findings

    def closest_item(self, old: CallableType, candidates: list[CallableType]) -> CallableType:
        def score(c: CallableType) -> tuple[int, int]:
            same_names = sum(1 for n in old.arg_names if n is not None and n in c.arg_names)
            return same_names, -abs(len(c.arg_types) - len(old.arg_types))

        return max(candidates, key=score)

    def incompatible(self, fullname: str, old: Type, new: Type) -> Finding:
        return Finding(
            "signature-incompatible",
            f"Signature of {quote(fullname)} changed incompatibly from "
            f"{quote(self.callable_text(old, self.old.options))} to "
            f"{quote(self.callable_text(new, self.new.options))}",
        )

    def callable_text(self, typ: Type, options: Options) -> str:
        proper = get_proper_type(typ)
        if isinstance(proper, CallableType):
            return pretty_callable(proper, options, skip_self=True)
        if isinstance(proper, Overloaded):
            return "; ".join(pretty_callable(t, options, skip_self=True) for t in proper.items)
        return format_type_bare(typ, options)

    def compare_signatures(
        self, fullname: str, old: CallableType, new: CallableType
    ) -> list[Finding]:
        saved = self.push_typevars(list(old.variables), list(new.variables))
        try:
            return self.compare_signatures_inner(fullname, old, new)
        finally:
            self.tv_map = saved

    def compare_signatures_inner(
        self, fullname: str, old: CallableType, new: CallableType
    ) -> list[Finding]:
        fn = quote(fullname)
        findings: list[Finding] = []
        used: set[int] = set()
        new_star = find_kind(new, ArgKind.ARG_STAR)
        new_star2 = find_kind(new, ArgKind.ARG_STAR2)

        for i, (kind, name) in enumerate(zip(old.arg_kinds, old.arg_names)):
            if kind.is_star():
                continue
            desc = param_desc(name, i)
            pos_capable = is_positional(kind)
            kw_capable = name is not None
            new_pos = find_positional(new, i) if pos_capable else None
            new_kw = find_by_name(new, name) if kw_capable else None
            target: int | None
            if pos_capable:
                if new_pos is None and new_star is None:
                    if new_kw is not None:
                        findings.append(
                            Finding(
                                "param-kind-changed",
                                f"{desc.capitalize()} of {fn} can no longer be passed "
                                "positionally",
                            )
                        )
                        target = new_kw
                    else:
                        findings.append(
                            Finding("param-removed", f"{desc.capitalize()} of {fn} was removed")
                        )
                        continue
                elif new_pos is None:
                    target = new_kw if new_kw is not None else new_star
                elif kw_capable and new_kw is None:
                    target = new_pos
                    new_name = new.arg_names[new_pos]
                    # Note that **kwargs doesn't make a rename safe: "f(old=...)" would
                    # silently go to **kwargs instead of the renamed parameter.
                    if new_name is None:
                        findings.append(
                            Finding(
                                "param-kind-changed",
                                f"{desc.capitalize()} of {fn} can no longer be passed by "
                                "keyword",
                            )
                        )
                    else:
                        findings.append(
                            Finding(
                                "param-renamed",
                                f"{desc.capitalize()} of {fn} was renamed to {quote(new_name)}",
                            )
                        )
                elif kw_capable and new_kw is not None and new_kw != new_pos:
                    target = new_kw
                    if is_positional(new.arg_kinds[new_kw]):
                        findings.append(
                            Finding(
                                "param-reordered",
                                f"{desc.capitalize()} of {fn} moved from position {i + 1} "
                                f"to {new_kw + 1}",
                            )
                        )
                    else:
                        findings.append(
                            Finding(
                                "param-kind-changed",
                                f"{desc.capitalize()} of {fn} can no longer be passed "
                                "positionally",
                            )
                        )
                else:
                    target = new_pos
            else:
                # Keyword-only in the old version
                if new_kw is None:
                    if new_star2 is None:
                        findings.append(
                            Finding("param-removed", f"{desc.capitalize()} of {fn} was removed")
                        )
                        continue
                    target = new_star2
                else:
                    target = new_kw

            assert target is not None
            used.add(target)
            findings.extend(
                self.compare_param_types(
                    f"{desc} of {fn}", old.arg_types[i], new.arg_types[target]
                )
            )
            if (
                not kind.is_required()
                and not new.arg_kinds[target].is_star()
                and new.arg_kinds[target].is_required()
            ):
                findings.append(
                    Finding(
                        "param-default-removed",
                        f"{desc.capitalize()} of {fn} no longer has a default value",
                    )
                )

        for star_kind, label in ((ArgKind.ARG_STAR, "*args"), (ArgKind.ARG_STAR2, "**kwargs")):
            old_index = find_kind(old, star_kind)
            if old_index is None:
                continue
            new_index = find_kind(new, star_kind)
            if new_index is None:
                findings.append(Finding("star-args-removed", f"{fn} no longer accepts {label}"))
                continue
            used.add(new_index)
            findings.extend(
                self.compare_param_types(
                    f"{label} of {fn}", old.arg_types[old_index], new.arg_types[new_index]
                )
            )

        for j, (kind, name) in enumerate(zip(new.arg_kinds, new.arg_names)):
            if j in used or kind.is_star():
                continue
            desc = param_desc(name, j)
            if kind.is_required():
                findings.append(Finding("param-added-required", f"{fn} has a new required {desc}"))
            else:
                findings.append(Finding("param-added-optional", f"{fn} has a new optional {desc}"))

        findings.extend(self.compare_return(fn, old, new))
        findings.extend(self.compare_function_typevars(fn, old, new))

        if not any(f.severity == BREAKING for f in findings):
            translated = get_proper_type(self.translate(old))
            with state.strict_optional_set(True):
                compatible = is_subtype(new, translated)
            if not compatible:
                findings.append(self.incompatible(fullname, old, new))
        return findings

    def compare_param_types(self, what: str, old_t: Type, new_t: Type) -> list[Finding]:
        result = self.check(old_t, new_t, CONTRAVARIANT)
        old_s = quote(self.fmt_old(old_t))
        new_s = quote(self.fmt_new(new_t))
        if result == BAD:
            verb = self.verb(old_t, new_t, wider="widened", narrower="narrowed")
            return [
                Finding("param-type-narrowed", f"Type of {what} {verb} from {old_s} to {new_s}")
            ]
        if result == OK:
            return [Finding("param-widened", f"Type of {what} widened from {old_s} to {new_s}")]
        return self.soft_findings(result, f"Type of {what}", old_s, new_s)

    def compare_return(self, fn: str, old: CallableType, new: CallableType) -> list[Finding]:
        old_ret = old.type_guard or old.type_is or old.ret_type
        new_ret = new.type_guard or new.type_is or new.ret_type
        result = self.check(old_ret, new_ret, COVARIANT)
        old_s = quote(self.fmt_old(old_ret))
        new_s = quote(self.fmt_new(new_ret))
        if result == BAD:
            verb = self.verb(old_ret, new_ret, wider="widened", narrower="narrowed")
            return [
                Finding("return-widened", f"Return type of {fn} {verb} from {old_s} to {new_s}")
            ]
        if result == OK:
            return [
                Finding("return-narrowed", f"Return type of {fn} narrowed from {old_s} to {new_s}")
            ]
        return self.soft_findings(result, f"Return type of {fn}", old_s, new_s)

    def compare_function_typevars(
        self, fn: str, old: CallableType, new: CallableType
    ) -> list[Finding]:
        new_by_name = {v.name: v for v in new.variables}
        findings = []
        for old_var in old.variables:
            new_var = new_by_name.get(old_var.name)
            if isinstance(old_var, TypeVarType) and isinstance(new_var, TypeVarType):
                findings.extend(
                    self.compare_typevar_bounds(
                        f"type variable {quote(old_var.name)} of {fn}", old_var, new_var
                    )
                )
        return findings

    # Generics

    def compare_typevar_bounds(
        self, what: str, old: TypeVarType, new: TypeVarType
    ) -> list[Finding]:
        if old.values or new.values:
            return self.compare_constraints(what, old, new)
        result = self.check(old.upper_bound, new.upper_bound, CONTRAVARIANT)
        old_s = quote(self.fmt_old(old.upper_bound))
        new_s = quote(self.fmt_new(new.upper_bound))
        if result == BAD:
            return [
                Finding(
                    "typeparam-bound-narrowed", f"Bound of {what} narrowed from {old_s} to {new_s}"
                )
            ]
        if result == OK:
            return [
                Finding(
                    "typeparam-bound-widened", f"Bound of {what} widened from {old_s} to {new_s}"
                )
            ]
        return []

    def compare_constraints(self, what: str, old: TypeVarType, new: TypeVarType) -> list[Finding]:
        old_values = old.values or [old.upper_bound]
        translated = [self.translate(v) for v in old_values]
        with state.strict_optional_set(True):
            if new.values:
                covered = all(any(is_subtype(o, n) for n in new.values) for o in translated)
            else:
                covered = all(is_subtype(o, new.upper_bound) for o in translated)
            same = len(old.values) == len(new.values) and all(
                any(is_same_type(o, n) for n in new.values) for o in translated
            )
        old_s = quote(", ".join(self.fmt_old(v) for v in old_values))
        new_s = quote(", ".join(self.fmt_new(v) for v in (new.values or [new.upper_bound])))
        if not covered:
            return [
                Finding(
                    "typeparam-bound-narrowed",
                    f"Constraints of {what} narrowed from {old_s} to {new_s}",
                )
            ]
        if not same:
            return [
                Finding(
                    "typeparam-bound-widened",
                    f"Constraints of {what} widened from {old_s} to {new_s}",
                )
            ]
        return []

    def compare_class_typevars(
        self, fullname: str, old_vars: list[TypeVarLikeType], new_vars: list[TypeVarLikeType]
    ) -> list[Finding]:
        cls = quote(fullname)
        findings = []
        new_index = {v.name: i for i, v in enumerate(new_vars)}
        old_names = {v.name for v in old_vars}
        for i, old_var in enumerate(old_vars):
            j = new_index.get(old_var.name)
            if j is None:
                findings.append(
                    Finding(
                        "typeparam-removed",
                        f"Type parameter {quote(old_var.name)} of {cls} was removed",
                    )
                )
                continue
            if j != i:
                findings.append(
                    Finding(
                        "typeparam-reordered",
                        f"Type parameter {quote(old_var.name)} of {cls} moved from position "
                        f"{i + 1} to {j + 1}",
                    )
                )
            new_var = new_vars[j]
            if isinstance(old_var, TypeVarType) and isinstance(new_var, TypeVarType):
                what = f"type parameter {quote(old_var.name)} of {cls}"
                findings.extend(self.compare_typevar_bounds(what, old_var, new_var))
                findings.extend(self.compare_variance(what, old_var.variance, new_var.variance))
        for new_var in new_vars:
            if new_var.name not in old_names:
                findings.append(
                    Finding(
                        "typeparam-added",
                        f"{cls} has a new type parameter {quote(new_var.name)}; "
                        f"unparameterized uses now mean {quote(new_var.name)} is Any",
                    )
                )
        return findings

    def compare_variance(self, what: str, old: int, new: int) -> list[Finding]:
        known = (INVARIANT, COVARIANT, CONTRAVARIANT)
        if old == new or old not in known or new not in known:
            return []
        if old == INVARIANT:
            # Invariant -> co/contravariant only allows more assignments for users.
            return []
        names = {INVARIANT: "invariant", COVARIANT: "covariant", CONTRAVARIANT: "contravariant"}
        return [
            Finding(
                "typeparam-variance-changed",
                f"Variance of {what} changed from {names[old]} to {names[new]}",
            )
        ]

    # Classes

    def is_package_class(self, info: TypeInfo) -> bool:
        return info.module_name.split(".")[0] in self.packages

    def compare_classes(
        self,
        fullname: str,
        old_info: TypeInfo,
        new_info: TypeInfo,
        module: str,
        old_loc: Location,
        new_loc: Location,
    ) -> None:
        key = (id(old_info), id(new_info))
        if key in self.compared_classes:
            return
        self.compared_classes.add(key)
        cls = quote(fullname)
        findings: list[Finding] = []

        if new_info.is_final and not old_info.is_final:
            findings.append(
                Finding("class-became-final", f"Class {cls} became final; it can't be subclassed")
            )

        new_mro = {base.fullname for base in new_info.mro}
        removed_bases = {
            base.fullname for base in old_info.mro[1:] if base.fullname not in new_mro
        }
        for base in old_info.mro[1:]:
            if base.fullname in removed_bases and not is_private_module(base.fullname):
                findings.append(
                    Finding(
                        "class-base-removed",
                        f"Class {cls} no longer inherits from {quote(base.fullname)}",
                    )
                )

        findings.extend(self.compare_metaclasses(cls, old_info, new_info))
        findings.extend(
            self.compare_class_typevars(fullname, old_info.defn.type_vars, new_info.defn.type_vars)
        )

        if old_info.is_protocol and not new_info.is_protocol:
            findings.append(
                Finding(
                    "class-no-longer-protocol",
                    f"{cls} is no longer a protocol; classes that matched it structurally "
                    "must now inherit from it",
                )
            )
        elif old_info.is_protocol and new_info.is_protocol:
            old_members = set(old_info.protocol_members)
            for member in new_info.protocol_members:
                if member not in old_members:
                    findings.append(
                        Finding(
                            "protocol-member-added",
                            f"Protocol {cls} has a new member {quote(member)}; existing "
                            "implementations may no longer match",
                        )
                    )
        else:
            old_abstract = {name for name, _ in old_info.abstract_attributes}
            for name, _ in new_info.abstract_attributes:
                if name not in old_abstract:
                    findings.append(
                        Finding(
                            "abstract-method-added",
                            f"{quote(fullname + '.' + name)} is abstract; subclasses of {cls} "
                            "must now implement it",
                        )
                    )

        if old_info.typeddict_type is not None and new_info.typeddict_type is not None:
            findings.extend(self.compare_typeddicts(fullname, old_info, new_info))
            self.add_findings(findings, fullname, old_loc, new_loc)
            return

        if not old_info.is_protocol and not new_info.is_protocol:
            findings.extend(self.compare_constructors(fullname, old_info, new_info))
        self.add_findings(findings, fullname, old_loc, new_loc)
        self.compare_members(fullname, old_info, new_info, module, removed_bases)

    def compare_metaclasses(
        self, cls: str, old_info: TypeInfo, new_info: TypeInfo
    ) -> list[Finding]:
        old_meta = old_info.metaclass_type
        new_meta = new_info.metaclass_type
        old_name = old_meta.type.fullname if old_meta else "builtins.type"
        new_name = new_meta.type.fullname if new_meta else "builtins.type"
        if old_name == new_name:
            return []
        if old_meta is None or new_meta is None:
            # Every metaclass is a subclass of type.
            related = True
        else:
            translated = self.translate(old_meta)
            with state.strict_optional_set(True):
                related = is_subtype(new_meta, translated) or is_subtype(translated, new_meta)
        if related:
            # Moving along the same metaclass hierarchy only breaks subclasses that combine
            # it with another, unrelated metaclass.
            return []
        return [
            Finding(
                "metaclass-changed",
                f"Metaclass of {cls} changed from {quote(old_name)} to {quote(new_name)}",
            )
        ]

    def compare_constructors(
        self, fullname: str, old_info: TypeInfo, new_info: TypeInfo
    ) -> list[Finding]:
        try:
            old_ctor = type_object_type(old_info)
            new_ctor = type_object_type(new_info)
        except Exception:
            return []
        if not isinstance(old_ctor, FunctionLike) or not isinstance(new_ctor, FunctionLike):
            return []
        findings = self.compare_callables(fullname, old_ctor, new_ctor)
        # The return type is the class itself, and its type variables are the class's type
        # parameters; both are compared at the class level.
        return [
            f
            for f in findings
            if f.code not in ("return-widened", "return-narrowed")
            and not f.code.startswith("typeparam-")
        ]

    def compare_typeddicts(
        self, fullname: str, old_info: TypeInfo, new_info: TypeInfo
    ) -> list[Finding]:
        old_td = old_info.typeddict_type
        new_td = new_info.typeddict_type
        assert old_td is not None and new_td is not None
        cls = quote(fullname)
        findings = []
        for key, old_type in old_td.items.items():
            if key not in new_td.items:
                findings.append(
                    Finding("symbol-removed", f"Key {quote(key)} of TypedDict {cls} was removed")
                )
                continue
            new_type = new_td.items[key]
            readonly = key in old_td.readonly_keys or key in new_td.readonly_keys
            result = self.check(old_type, new_type, COVARIANT if readonly else INVARIANT)
            what = f"key {quote(key)} of TypedDict {cls}"
            old_s = quote(self.fmt_old(old_type))
            new_s = quote(self.fmt_new(new_type))
            if result == BAD:
                code = "attr-widened" if readonly else "attr-type-changed"
                findings.append(Finding(code, f"Type of {what} changed from {old_s} to {new_s}"))
            else:
                findings.extend(self.soft_findings(result, f"Type of {what}", old_s, new_s))
            if key in new_td.required_keys and key not in old_td.required_keys:
                findings.append(
                    Finding("typeddict-key-required", f"Key {quote(key)} of {cls} is now required")
                )
        for key in new_td.items:
            if key not in old_td.items and key in new_td.required_keys:
                findings.append(
                    Finding("typeddict-key-required", f"{cls} has a new required key {quote(key)}")
                )
        return findings

    def compare_members(
        self,
        class_fullname: str,
        old_info: TypeInfo,
        new_info: TypeInfo,
        module: str,
        removed_bases: set[str],
    ) -> None:
        saved = self.push_typevars(old_info.defn.type_vars, new_info.defn.type_vars)
        try:
            self.compare_member_pairs(class_fullname, old_info, new_info, module, removed_bases)
        finally:
            self.tv_map = saved

    def compare_member_pairs(
        self,
        class_fullname: str,
        old_info: TypeInfo,
        new_info: TypeInfo,
        module: str,
        removed_bases: set[str],
    ) -> None:
        old_members = class_members(old_info)
        new_members = class_members(new_info)
        for name, old_member in old_members.items():
            fullname = f"{class_fullname}.{name}"
            if name in ("__init__", "__new__"):
                # Covered by the constructor comparison.
                continue
            new_member = new_members.get(name)
            old_view = self.view(old_member.node, old_info, self.old, old_member.defining_info)
            old_loc = self.member_loc(self.old, old_member, module, old_view)
            if new_member is None:
                defining = old_member.defining_info
                if defining.fullname in removed_bases and not self.is_package_class(defining):
                    # Inherited from an external base that was removed; reported there.
                    continue
                self.add(
                    "symbol-removed", fullname, f"{quote(fullname)} was removed", old_loc, None
                )
                continue
            if (
                old_member.defining_info.fullname == new_member.defining_info.fullname
                and not self.is_package_class(new_member.defining_info)
            ):
                # Inherited unchanged from a class outside the package.
                continue
            self.symbols_checked += 1
            new_view = self.view(new_member.node, new_info, self.new, new_member.defining_info)
            new_loc = self.member_loc(self.new, new_member, module, new_view)
            if old_view.kind == CLASS and new_view.kind == CLASS:
                assert old_view.info is not None and new_view.info is not None
                self.compare_classes(
                    fullname, old_view.info, new_view.info, module, old_loc, new_loc
                )
                continue
            findings = self.compare_views(fullname, old_view, new_view, in_class=True)
            if (
                new_view.final
                and not old_view.final
                and new_view.kind == CALLABLE
                and not new_info.is_final
            ):
                findings.append(
                    Finding(
                        "method-became-final",
                        f"Method {quote(fullname)} became final; subclasses can no longer "
                        "override it",
                    )
                )
            self.add_findings(findings, fullname, old_loc, new_loc)

        for name, new_member in new_members.items():
            if name in old_members or name in ("__init__", "__new__"):
                continue
            if not self.is_package_class(new_member.defining_info):
                continue
            new_view = self.view(new_member.node, new_info, self.new, new_member.defining_info)
            fullname = f"{class_fullname}.{name}"
            self.add(
                "symbol-added",
                fullname,
                f"{describe_kind(new_view).capitalize()} {quote(fullname)} was added",
                None,
                self.member_loc(self.new, new_member, module, new_view),
            )


def article(noun: str | None) -> str:
    if noun is None:
        return "something else"
    return ("an " if noun[0] in "aeiou" else "a ") + noun


def compare_versions(old: BuiltVersion, new: BuiltVersion) -> tuple[list[Change], int]:
    """Compare two built versions. Returns the changes and the number of symbols checked."""
    comparer = ApiComparer(old, new)
    changes = comparer.run()
    return changes, comparer.symbols_checked
