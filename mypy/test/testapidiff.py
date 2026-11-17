"""Tests for apidiff (breaking change detection between two package versions)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import textwrap
import unittest
from collections.abc import Iterator

from mypy.apidiff import (
    Change,
    compare_api,
    format_markdown,
    format_text,
    load_api,
    main,
    make_options,
    parse_options,
)

OLD = """
from typing import Any, Final, Generic, TypeVar, overload

T = TypeVar("T")

def read(name: str) -> str: ...
def write(path: str, data: bytes, mode: str = "w") -> None: ...
def narrow_param(x: int | str) -> None: ...
def removed() -> None: ...
def star(*args: int, **kwargs: int) -> None: ...
def pos_only(a: int, /) -> None: ...
def kw_only(*, a: int, b: int) -> None: ...
def returns_narrower(x: int) -> int | None: ...
def widens_param(x: int) -> None: ...
def gets_optional_param(x: int) -> None: ...
def to_any() -> int: ...
def _private() -> None: ...
def untyped(a, b=2): pass

@overload
def ov(x: int) -> int: ...
@overload
def ov(x: str) -> str: ...
def ov(x: Any) -> Any: return x

class Box(Generic[T]):
    value: int
    limit: int
    name: str
    def get(self) -> T: ...
    @property
    def size(self) -> int: ...
    def gone(self) -> None: ...

class Base: ...
class Child(Base): ...
class Plain: ...
class RemovedClass:
    def method(self) -> None: ...

attr: int = 0
Alias = list[int]
"""

NEW = """
from typing import Any, Final, Generic, TypeVar, final, overload

T = TypeVar("T")
S = TypeVar("S")

def read(name: str) -> str | None: ...
def write(target: str, data: bytes, *, mode: str, extra: int) -> None: ...
def narrow_param(x: int) -> None: ...
def star() -> None: ...
def pos_only(renamed: int, /) -> None: ...
def kw_only(*, b: int, a: int) -> None: ...
def returns_narrower(x: int) -> int: ...
def widens_param(x: int | str) -> None: ...
def gets_optional_param(x: int, y: int = 0) -> None: ...
def to_any() -> Any: ...
def untyped(a): pass
def added() -> None: ...

@overload
def ov(x: int) -> int: ...
@overload
def ov(x: bytes) -> str: ...
def ov(x: Any) -> Any: return x

class Box(Generic[T, S]):
    value: int | None
    limit: Final[int] = 0
    name: str
    def get(self) -> T: ...
    @property
    def size(self) -> int | None: ...

class Base: ...
@final
class Child: ...
class Plain: ...

def attr() -> int: ...
Alias = list[str]
"""

EXPECTED = {
    ("pkg.read", "return-type"),
    ("pkg.write", "param-renamed"),
    ("pkg.write", "param-kind"),
    ("pkg.write", "param-default"),
    ("pkg.write", "param-added"),
    ("pkg.narrow_param", "param-type"),
    ("pkg.removed", "removed"),
    ("pkg.star", "param-removed"),
    ("pkg.untyped", "param-removed"),
    ("pkg.ov", "overload"),
    ("pkg.Box", "generic"),
    ("pkg.Box.value", "attribute-type"),
    ("pkg.Box.limit", "read-only"),
    ("pkg.Box.size", "attribute-type"),
    ("pkg.Box.gone", "removed"),
    ("pkg.Child", "base-removed"),
    ("pkg.Child", "final"),
    ("pkg.RemovedClass", "removed"),
    ("pkg.attr", "kind-changed"),
    ("pkg.Alias", "alias"),
}


@contextlib.contextmanager
def package_versions(old: str, new: str) -> Iterator[tuple[str, str]]:
    """Write two versions of a package "pkg" and yield the two directories."""
    with tempfile.TemporaryDirectory() as tmp:
        dirs = []
        for version, source in (("old", old), ("new", new)):
            os.makedirs(os.path.join(tmp, version, "pkg"))
            with open(os.path.join(tmp, version, "pkg", "__init__.py"), "w") as f:
                f.write(textwrap.dedent(source))
            dirs.append(os.path.join(tmp, version))
        yield dirs[0], dirs[1]


def run_main(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = main(list(args))
    return status, out.getvalue()


class APIDiffSuite(unittest.TestCase):
    changes: list[Change]

    @classmethod
    def setUpClass(cls) -> None:
        options = make_options(parse_options(["old", "new"]))
        with package_versions(OLD, NEW) as (old, new):
            cls.changes = compare_api(load_api(old, options), load_api(new, options))

    def test_breaking_changes_are_found(self) -> None:
        found = {(c.name, c.code) for c in self.changes}
        assert found == EXPECTED, (found - EXPECTED, EXPECTED - found)

    def test_wider_return_type_example(self) -> None:
        (change,) = [c for c in self.changes if c.name == "pkg.read"]
        assert (change.old, change.new) == ("str", "str | None")

    def test_compatible_changes_are_not_reported(self) -> None:
        names = {c.name for c in self.changes}
        for name in (
            "pkg.pos_only",  # Positional-only parameters may be renamed
            "pkg.kw_only",  # Keyword-only parameters may be reordered
            "pkg.returns_narrower",
            "pkg.widens_param",
            "pkg.gets_optional_param",
            "pkg.to_any",
            "pkg._private",
            "pkg.Box.get",  # "self" changes with the class, but isn't passed by callers
            "pkg.Box.name",
            "pkg.Plain",
            "pkg.RemovedClass.method",  # Covered by the removal of the class
        ):
            assert name not in names, name

    def test_text_and_markdown_reports(self) -> None:
        change = Change("pkg.f", "return-type", "Wider", "int", "int | None")
        assert format_text([change]) == (
            'pkg.f: Wider (old: "int", new: "int | None")  [return-type]\n'
            "Found 1 breaking change"
        )
        assert format_markdown([change]).splitlines()[-1] == (
            "| `pkg.f` | `return-type` | Wider | `int` | `int \\| None` |"
        )
        assert format_markdown([]) == "## API changes: 0 breaking changes"


class APIDiffCommandLineSuite(unittest.TestCase):
    def test_exit_status_and_json(self) -> None:
        old = "def read(name: str) -> str: ...\ndef keep() -> int: ...\n"
        new = "def read(name: str) -> str | None: ...\ndef keep() -> int: ...\n"
        with package_versions(old, new) as (old_dir, new_dir):
            status, out = run_main(old_dir, new_dir, "--format", "json")
            assert status == 1
            report = json.loads(out)
            assert report["count"] == 1
            assert report["breaking_changes"][0]["code"] == "return-type"

            assert run_main(old_dir, new_dir, "--ignore", r"pkg\.read")[0] == 0
            assert run_main(old_dir, old_dir)[0] == 0
            assert run_main(old_dir, os.path.join(new_dir, "missing"))[0] == 2
            # Neither version was modified (e.g. no cache was written).
            assert sorted(os.listdir(old_dir)) == ["pkg"]
            assert os.listdir(os.path.join(old_dir, "pkg")) == ["__init__.py"]
