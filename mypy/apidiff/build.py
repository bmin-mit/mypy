"""Build one version of a package with mypy, without writing anything to disk."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import mypy.build
import mypy.modulefinder
from mypy.errors import CompileError
from mypy.modulefinder import BuildSource, FindModuleCache
from mypy.nodes import MypyFile
from mypy.options import Options

# Top-level entries of a source root that are never treated as packages to compare.
IGNORED_TOP_LEVEL = frozenset({"setup", "conftest", "noxfile", "test", "tests", "docs"})


class ApiDiffError(Exception):
    """A problem that prevents the comparison from running (exit code 2)."""


@dataclass
class BuiltVersion:
    """The result of building one version of the package(s) under comparison."""

    # "old" or "new"
    label: str
    # The root directory as given by the user (used when printing paths)
    root: str
    # Absolute root directory
    abs_root: str
    # Every module mypy processed, including stdlib and dependencies
    modules: dict[str, MypyFile]
    # Modules that belong to the packages under comparison, sorted
    package_modules: list[str]
    # Top-level packages (of those requested) that exist in this version
    packages: list[str]
    # Imports that could not be resolved anywhere in the build
    missing_imports: set[str] = field(default_factory=set)
    options: Options = field(default_factory=Options)

    def display_path(self, path: str) -> str:
        """Turn a path recorded by mypy into one relative to the user's root argument."""
        abs_path = os.path.abspath(path)
        if abs_path.startswith(self.abs_root + os.sep):
            return os.path.join(self.root, os.path.relpath(abs_path, self.abs_root))
        return path


def discover_packages(root: str) -> list[str]:
    """Find top-level packages and modules in a source root."""
    result = set()
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            name = entry
            if not any(
                os.path.isfile(os.path.join(full, init))
                for init in ("__init__.py", "__init__.pyi")
            ):
                continue
        elif entry.endswith((".py", ".pyi")):
            name = entry.rsplit(".", 1)[0]
        else:
            continue
        if not name.isidentifier() or name.startswith("_") or name in IGNORED_TOP_LEVEL:
            continue
        result.add(name)
    return sorted(result)


def prepare_options(options: Options, root: str) -> Options:
    """Configure fresh options for a read-only build of root."""
    options.incremental = False
    # This is the documented way of disabling the cache entirely.
    options.cache_dir = os.devnull
    options.implicit_reexport = False
    # Put the root first so that an installed copy of the package can't shadow it.
    options.mypy_path = [root] + [p for p in options.mypy_path if os.path.abspath(p) != root]
    # We never report the package's own type errors, but analysis must still complete.
    options.allow_empty_bodies = True
    return options


def build_version(
    label: str, root: str, packages: list[str], make_options: Callable[[], Options]
) -> BuiltVersion:
    """Run mypy over the given packages located in root.

    Packages that don't exist in this version are skipped (they show up as removed or
    added). Raises ApiDiffError if mypy reports a blocking error, e.g. a syntax error.
    """
    abs_root = os.path.abspath(root)
    if not os.path.isdir(abs_root):
        raise ApiDiffError(f"{label} version: directory not found: {root}")
    options = prepare_options(make_options(), abs_root)
    data_dir = mypy.build.default_data_dir()
    search_paths = mypy.modulefinder.compute_search_paths([], options, data_dir)
    finder = FindModuleCache(search_paths, fscache=None, options=options)

    sources: list[BuildSource] = []
    found_packages = []
    for package in packages:
        found = [
            BuildSource(s.path, s.module, None, base_dir=abs_root)
            for s in finder.find_modules_recursive(package)
            if s.path is not None and os.path.abspath(s.path).startswith(abs_root + os.sep)
        ]
        if found:
            found_packages.append(package)
            sources.extend(found)

    if not sources:
        return BuiltVersion(label, root, abs_root, {}, [], [], options=options)

    try:
        result = mypy.build.build(sources=sources, options=options)
    except CompileError as e:
        raise ApiDiffError(f"{label} version failed to build:\n" + "\n".join(e.messages)) from e

    package_modules = sorted({s.module for s in sources if s.module in result.files})
    missing = set(result.manager.missing_modules)
    return BuiltVersion(
        label,
        root,
        abs_root,
        result.files,
        package_modules,
        found_packages,
        missing_imports=missing,
        options=options,
    )
