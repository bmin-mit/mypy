"""apidiff: detect breaking changes between two versions of a package's typed API.

Usage:
    apidiff --old path/to/v1 --new path/to/v2 [-p PACKAGE ...]

Both versions are analyzed with mypy (read-only; no cache is written) and their public
APIs are compared: functions, classes, methods, attributes, generics and overloads.
Each difference is classified as breaking, warning or info, and reported as mypy-style
text or JSON.

Exit status: 0 if no breaking changes were found, 1 if breaking changes were found (or
warnings, with --strict), 2 if the comparison couldn't run.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import TextIO

from mypy.apidiff.allowlist import Allowlist, generate_allowlist
from mypy.apidiff.build import ApiDiffError, BuiltVersion, build_version, discover_packages
from mypy.apidiff.compare import BREAKING, CODES, WARNING, Change, compare_versions
from mypy.apidiff.report import render_json, render_text
from mypy.config_parser import parse_config_file, parse_version
from mypy.options import Options

__all__ = ["main", "run", "compare_trees", "ApiDiffError"]

EXIT_OK = 0
EXIT_BREAKING = 1
EXIT_ERROR = 2


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apidiff",
        description="Compare the public typed API of two versions of a package and report "
        "changes that could break existing code.",
        epilog="Exit status: 0 = no breaking changes, 1 = breaking changes found (or "
        "warnings with --strict), 2 = error.",
    )
    parser.add_argument(
        "--old", required=True, metavar="DIR", help="source root of the old version"
    )
    parser.add_argument(
        "--new", required=True, metavar="DIR", help="source root of the new version"
    )
    parser.add_argument(
        "-p",
        "--package",
        action="append",
        dest="packages",
        default=[],
        metavar="PACKAGE",
        help="top-level package or module to compare (repeatable; default: every package "
        "found in either root)",
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="output format (default: text)"
    )
    parser.add_argument("-o", "--output", metavar="FILE", help="write the report to FILE")
    parser.add_argument(
        "--strict", action="store_true", help="also exit with status 1 if there are warnings"
    )
    parser.add_argument(
        "--show-info",
        action="store_true",
        help="include informational changes (additions, widened parameters) in text output",
    )
    parser.add_argument(
        "--allowlist",
        action="append",
        default=[],
        metavar="FILE",
        help="file of regexes matching accepted changes as 'fullname' or 'fullname:code' "
        "(repeatable)",
    )
    parser.add_argument(
        "--generate-allowlist",
        action="store_true",
        help="print an allowlist accepting all current breaking changes and warnings",
    )
    parser.add_argument(
        "--ignore-unused-allowlist",
        action="store_true",
        help="don't warn about allowlist entries that match nothing",
    )
    parser.add_argument(
        "--python-version", metavar="X.Y", help="Python version to analyze the packages for"
    )
    parser.add_argument(
        "--python-executable",
        metavar="EXECUTABLE",
        help="Python interpreter whose site-packages provide the dependencies",
    )
    parser.add_argument("--mypy-config-file", metavar="FILE", help="use this mypy config file")
    parser.add_argument("--custom-typeshed-dir", metavar="DIR", help="use a custom typeshed")
    parser.add_argument("--list-codes", action="store_true", help="list all change codes and exit")
    parser.add_argument("--show-traceback", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def options_factory(args: argparse.Namespace) -> Options:
    options = Options()
    if args.mypy_config_file:

        def set_strict_flags() -> None:
            return

        parse_config_file(options, set_strict_flags, args.mypy_config_file, sys.stdout, sys.stderr)
    if args.python_version:
        options.python_version = parse_version(args.python_version)
    if args.python_executable:
        options.python_executable = args.python_executable
    if args.custom_typeshed_dir:
        options.custom_typeshed_dir = args.custom_typeshed_dir
        options.abs_custom_typeshed_dir = os.path.abspath(args.custom_typeshed_dir)
    options.show_traceback = args.show_traceback
    return options


def is_inside(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def compare_trees(
    old_root: str,
    new_root: str,
    packages: list[str] | None = None,
    make_options: Callable[[], Options] | None = None,
) -> tuple[list[Change], int, BuiltVersion, BuiltVersion]:
    """Build and compare two source trees. Returns (changes, symbols_checked, old, new)."""
    factory = make_options or Options
    for label, root in (("old", old_root), ("new", new_root)):
        if not os.path.isdir(root):
            raise ApiDiffError(f"{label} version: directory not found: {root}")
    if not packages:
        packages = sorted(set(discover_packages(old_root)) | set(discover_packages(new_root)))
        if not packages:
            raise ApiDiffError("no packages found; pass them with -p")
    old = build_version("old", old_root, packages, factory)
    new = build_version("new", new_root, packages, factory)
    missing = [p for p in packages if p not in old.packages and p not in new.packages]
    if missing:
        raise ApiDiffError("package(s) not found in either version: " + ", ".join(sorted(missing)))
    changes, checked = compare_versions(old, new)
    return changes, checked, old, new


def run(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    if args.list_codes:
        for code, (severity, description) in CODES.items():
            stdout.write(f"{code:28} {severity:9} {description}\n")
        return EXIT_OK

    for root in (args.old, args.new):
        if args.output and is_inside(args.output, root):
            raise ApiDiffError(f"refusing to write the report inside a compared tree: {root}")

    allowlist = Allowlist.from_files(args.allowlist)

    changes, checked, _, _ = compare_trees(
        args.old, args.new, args.packages, lambda: options_factory(args)
    )

    if args.generate_allowlist:
        write_output(generate_allowlist(changes), args.output, stdout)
        return EXIT_OK

    changes = allowlist.filter(changes)
    if not args.ignore_unused_allowlist:
        for entry in allowlist.unused():
            changes.append(
                Change(
                    "unused-allowlist",
                    WARNING,
                    entry.pattern,
                    f"Allowlist entry {entry.pattern!r} didn't match any change",
                    None,
                    None,
                )
            )

    if args.format == "json":
        report = render_json(
            changes, old_root=args.old, new_root=args.new, symbols_checked=checked
        )
    else:
        report = render_text(
            changes, fallback_path=args.new, show_info=args.show_info, symbols_checked=checked
        )
    write_output(report, args.output, stdout)

    if any(c.severity == BREAKING for c in changes):
        return EXIT_BREAKING
    if args.strict and any(c.severity == WARNING for c in changes):
        return EXIT_BREAKING
    return EXIT_OK


def write_output(text: str, output: str | None, stdout: TextIO) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        stdout.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args, sys.stdout, sys.stderr)
    except (ApiDiffError, ValueError, OSError) as e:
        if args.show_traceback:
            traceback.print_exc()
        sys.stderr.write(f"apidiff: error: {e}\n")
        return EXIT_ERROR
    except Exception:
        traceback.print_exc()
        sys.stderr.write("apidiff: error: internal error (please report this as a bug)\n")
        return EXIT_ERROR
