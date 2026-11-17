"""Find breaking changes between two versions of a package's public API.

Both versions are analyzed with mypy (without writing a cache, so neither is
modified). Every public symbol of the old version is then looked up in the new
version and the two are compared:

* removed symbols are breaking;
* a function may not drop, rename, reorder or make required any parameter that
  callers could pass, may not accept fewer parameter types and may not return
  more types (for example ``-> str`` becoming ``-> str | None``);
* a class may not lose a base class, become final or abstract, or change the
  number of its type parameters; its members are compared like module members;
* a mutable attribute must keep its type, a read-only one may only get narrower.

Types come from two separate builds, so they can't be compared with
``is_subtype``. Instead they are compared by their printed form, with unions
split into items. Any other difference is reported (conservatively) as breaking.
"""

from __future__ import annotations

from dataclasses import dataclass

import mypy.build
from mypy.find_sources import create_source_list
from mypy.nodes import (
    Decorator,
    FuncDef,
    MypyFile,
    OverloadedFuncDef,
    SymbolNode,
    SymbolTable,
    TypeAlias,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.typeops import callable_type
from mypy.types import (
    AnyType,
    CallableType,
    FormalArgument,
    Instance,
    Overloaded,
    Type,
    UnionType,
    get_proper_type,
)


@dataclass(frozen=True)
class Change:
    """A single breaking change, as shown in reports."""

    name: str  # Fully qualified name of the affected symbol
    code: str  # Short machine-readable kind of change, e.g. "return-type"
    message: str  # Human-readable explanation
    old: str | None = None  # Old type/signature, if relevant
    new: str | None = None  # New type/signature, if relevant


@dataclass
class Entry:
    """A public symbol, reduced to what matters for compatibility."""

    kind: str  # "function", "class", "attribute", "type alias" or "symbol"
    node: SymbolNode
    sigs: list[CallableType]  # Signatures (one per overload item) of a function
    type: Type | None = None  # Type of an attribute or target of a type alias
    read_only: bool = False  # Whether an attribute can't be assigned to


# Loading and snapshotting the public API


def load_api(path: str, options: Options) -> dict[str, Entry]:
    """Analyze the package at ``path`` and return its public API by full name.

    Type errors in the package are ignored. Raises CompileError on blocking errors.
    """
    sources = create_source_list([path], options)
    result = mypy.build.build(sources, options)
    function_info = result.files["builtins"].names["function"].node
    assert isinstance(function_info, TypeInfo)
    fallback = Instance(function_info, [])
    packages = {source.module.split(".")[0] for source in sources}
    api: dict[str, Entry] = {}
    for source in sources:
        _snapshot(source.module, result.files[source.module].names, packages, fallback, api)
    return api


def is_public(name: str, in_class: bool) -> bool:
    """Private names start with an underscore; dunders are public in classes."""
    if in_class and name.startswith("__") and name.endswith("__"):
        return True
    return not name.startswith("_")


def _snapshot(
    prefix: str,
    names: SymbolTable,
    packages: set[str],
    fallback: Instance,
    api: dict[str, Entry],
    in_class: bool = False,
) -> None:
    for name, sym in names.items():
        node = sym.node
        if node is None or isinstance(node, MypyFile) or not is_public(name, in_class):
            continue
        if not in_class and (sym.module_hidden or not sym.module_public):
            continue
        if node.fullname.split(".")[0] not in packages:
            continue  # Imported from another package (e.g. "from typing import Any")
        fullname = f"{prefix}.{name}"
        api[fullname] = _make_entry(node, fallback, in_class)
        # Recurse into classes only where they are defined, not where re-exported.
        if isinstance(node, TypeInfo) and node.fullname == fullname:
            _snapshot(fullname, node.names, packages, fallback, api, in_class=True)


def _make_entry(node: SymbolNode, fallback: Instance, in_class: bool) -> Entry:
    if isinstance(node, TypeInfo):
        return Entry("class", node, [])
    if isinstance(node, TypeAlias):
        return Entry("type alias", node, [], node.target)
    if isinstance(node, Var):
        return Entry("attribute", node, [], node.type, read_only=node.is_final)
    if isinstance(node, (FuncDef, Decorator, OverloadedFuncDef)):
        sigs = _signatures(node, fallback)
        func = node.func if isinstance(node, Decorator) else node
        if in_class and func.has_self_or_cls_argument:
            # Callers never pass "self"/"cls" explicitly, so leave them out.
            sigs = [_drop_first_arg(sig) for sig in sigs]
        if func.is_property:
            settable = isinstance(node, OverloadedFuncDef)  # A property with a setter
            typ = sigs[0].ret_type if sigs else None
            return Entry("attribute", node, [], typ, read_only=not settable)
        if isinstance(node, Decorator) and node.var.type is not None and not sigs:
            # Decorated into something that isn't callable in a known way.
            return Entry("attribute", node, [], node.var.type, read_only=True)
        return Entry("function", node, sigs)
    return Entry("symbol", node, [])


def _signatures(
    node: FuncDef | Decorator | OverloadedFuncDef, fallback: Instance
) -> list[CallableType]:
    typ = get_proper_type(node.var.type if isinstance(node, Decorator) else node.type)
    if isinstance(typ, Overloaded):
        return list(typ.items)
    if isinstance(typ, CallableType):
        return [typ]
    if isinstance(node, Decorator) and node.var.type is not None:
        return []
    func = node.func if isinstance(node, Decorator) else node
    if isinstance(func, FuncDef):
        return [callable_type(func, fallback)]  # Unannotated: every type is Any
    return []


def _drop_first_arg(sig: CallableType) -> CallableType:
    if not sig.arg_kinds or not sig.arg_kinds[0].is_positional():
        return sig
    return sig.copy_modified(
        arg_types=sig.arg_types[1:], arg_kinds=sig.arg_kinds[1:], arg_names=sig.arg_names[1:]
    )


# Comparing two snapshots


def compare_api(old: dict[str, Entry], new: dict[str, Entry]) -> list[Change]:
    """Return all breaking changes from ``old`` to ``new``, sorted by name."""
    changes: list[Change] = []
    removed: list[str] = []
    for name in sorted(old):
        if any(name.startswith(r + ".") for r in removed):
            continue  # Already reported the removal of the enclosing class
        before, after = old[name], new.get(name)
        if after is None:
            removed.append(name)
            changes.append(Change(name, "removed", f"{before.kind.capitalize()} was removed"))
        elif before.kind != after.kind:
            msg = f"Changed from a {before.kind} to a {after.kind}"
            changes.append(Change(name, "kind-changed", msg))
        elif before.kind == "function":
            changes += compare_functions(name, before.sigs, after.sigs)
        elif before.kind == "class":
            assert isinstance(before.node, TypeInfo) and isinstance(after.node, TypeInfo)
            changes += compare_classes(name, before.node, after.node)
        elif before.kind == "attribute":
            changes += compare_attributes(name, before, after)
        elif before.kind == "type alias" and not _same_type(before.type, after.type):
            msg = "Type alias now refers to a different type"
            changes.append(Change(name, "alias", msg, _fmt(before.type), _fmt(after.type)))
    return changes


def compare_functions(name: str, old: list[CallableType], new: list[CallableType]) -> list[Change]:
    if len(old) == 1 and len(new) == 1:
        return compare_signatures(name, old[0], new[0])
    # Overloads: every old way of calling must still be accepted by some new item.
    changes = []
    for sig in old:
        if not any(not compare_signatures(name, sig, candidate) for candidate in new):
            msg = "No compatible signature for this call any more"
            changes.append(Change(name, "overload", msg, str(sig), _fmt_sigs(new)))
    return changes


def compare_signatures(name: str, old: CallableType, new: CallableType) -> list[Change]:
    """Compare two callables: can every old call still be made, with no surprises?"""
    changes = []

    def report(code: str, message: str, before: object = None, after: object = None) -> None:
        old_s = None if before is None else str(before)
        new_s = None if after is None else str(after)
        changes.append(Change(name, code, message, old_s, new_s))

    for arg in old.formal_arguments():
        label = f'"{arg.name}"' if arg.name else f"#{(arg.pos or 0) + 1}"
        by_pos = new.argument_by_position(arg.pos)
        by_name = new.argument_by_name(arg.name)
        if arg.pos is not None and by_pos is None:
            if by_name is None:
                report("param-removed", f"Parameter {label} was removed")
                continue
            report("param-kind", f"Parameter {label} is now keyword-only")
        elif arg.name is not None and by_name is None:
            report("param-renamed", f"Parameter {label} can no longer be passed by keyword")
        elif by_pos is not None and by_pos.name is not None and by_pos.name != arg.name:
            report("param-moved", f"Parameter {label} changed position")
        targets = [by_pos] if by_name is None or by_name == by_pos else [by_pos, by_name]
        for target in [t for t in targets if t is not None]:
            if not arg.required and target.required:
                report("param-default", f"Parameter {label} no longer has a default value")
            if not is_compatible(arg.typ, target.typ, covariant=False):
                report("param-type", f"Parameter {label} accepts fewer types", arg.typ, target.typ)
    for kind, star in (("*args", old.var_arg()), ("**kwargs", old.kw_arg())):
        new_star = new.var_arg() if kind == "*args" else new.kw_arg()
        if star is not None and new_star is None:
            report("param-removed", f"Parameter {kind} was removed")
        elif star and new_star and not is_compatible(star.typ, new_star.typ, covariant=False):
            report("param-type", f"Parameter {kind} accepts fewer types", star.typ, new_star.typ)
    for arg in new.formal_arguments():
        if arg.required and not any(_corresponds(a, arg) for a in old.formal_arguments()):
            report("param-added", f'New required parameter "{arg.name}"')
    if not is_compatible(old.ret_type, new.ret_type, covariant=True):
        report(
            "return-type",
            "Return type is wider, callers may get values they don't expect",
            old.ret_type,
            new.ret_type,
        )
    return changes


def compare_classes(name: str, old: TypeInfo, new: TypeInfo) -> list[Change]:
    changes = []
    new_mro = {base.fullname for base in new.mro}
    for base in old.mro[1:]:
        if base.fullname not in new_mro:
            msg = f'No longer a subclass of "{base.fullname}"'
            changes.append(Change(name, "base-removed", msg))
    if new.is_final and not old.is_final:
        changes.append(Change(name, "final", "Class is now final and can't be subclassed"))
    if new.is_abstract and not old.is_abstract:
        changes.append(Change(name, "abstract", "Class is now abstract and can't be instantiated"))
    if len(old.type_vars) != len(new.type_vars):
        changes.append(
            Change(
                name,
                "generic",
                "Number of type parameters changed",
                ", ".join(old.type_vars),
                ", ".join(new.type_vars),
            )
        )
    return changes


def compare_attributes(name: str, old: Entry, new: Entry) -> list[Change]:
    changes = []
    if new.read_only and not old.read_only:
        changes.append(Change(name, "read-only", "Attribute can no longer be assigned to"))
    # Values are read from a read-only attribute (covariant), but also written to a
    # mutable one, so then the type must not change at all (invariant).
    ok = is_compatible(old.type, new.type, covariant=True)
    if not old.read_only and not new.read_only:
        ok = ok and is_compatible(old.type, new.type, covariant=False)
    if not ok:
        msg = "Attribute type changed"
        changes.append(Change(name, "attribute-type", msg, _fmt(old.type), _fmt(new.type)))
    return changes


def _corresponds(old: FormalArgument, new: FormalArgument) -> bool:
    """Could a caller of the old signature already be passing ``new``?"""
    same_name = old.name is not None and old.name == new.name
    return same_name or (new.pos is not None and old.pos == new.pos)


# Type compatibility


def is_compatible(old: Type | None, new: Type | None, covariant: bool) -> bool:
    """Can code written against ``old`` keep working with ``new``?

    If ``covariant``, values flow out to callers (return types, read-only attributes),
    so ``new`` may not add union items. Otherwise values flow in (parameters), so
    ``new`` may not drop union items. Missing annotations and ``Any`` always match.
    """
    if old is None or new is None:
        return True
    if isinstance(get_proper_type(old), AnyType) or isinstance(get_proper_type(new), AnyType):
        return True
    narrow, wide = (new, old) if covariant else (old, new)
    narrow_items, wide_items = _union_items(narrow), _union_items(wide)
    return "builtins.object" in wide_items or wide_items >= narrow_items


def _union_items(typ: Type) -> set[str]:
    proper = get_proper_type(typ)
    if isinstance(proper, UnionType):
        return {item for t in proper.items for item in _union_items(t)}
    if isinstance(proper, Instance) and proper.type.fullname == "builtins.object":
        return {"builtins.object"}
    return {str(proper)}


def _same_type(old: Type | None, new: Type | None) -> bool:
    return is_compatible(old, new, covariant=True) and is_compatible(old, new, covariant=False)


def _fmt(typ: Type | None) -> str | None:
    return None if typ is None else str(typ)


def _fmt_sigs(sigs: list[CallableType]) -> str:
    return "; ".join(str(sig) for sig in sigs)
