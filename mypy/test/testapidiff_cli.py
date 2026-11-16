"""Tests for the apidiff command line: exit codes, JSON output, allowlists, read-only runs."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import textwrap
import unittest

from mypy.apidiff import main
from mypy.apidiff.compare import CODES, SEVERITIES

OLD = """\
def read(name: str) -> str: ...
def keep(x: int) -> int: ...
"""

NEW_BREAKING = """\
def read(name: str) -> str | None: ...
def keep(x: int) -> int: ...
"""

NEW_WARNING = """\
from typing import Any
def read(name: str) -> str: ...
def keep(x: int) -> Any: ...
"""


class ApiDiffCliSuite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.old = os.path.join(self.tmp, "old")
        self.new = os.path.join(self.tmp, "new")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, root: str, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(textwrap.dedent(content))

    def setup_versions(self, old: str = OLD, new: str = NEW_BREAKING) -> None:
        self.write(self.old, {"pkg/__init__.py": old})
        self.write(self.new, {"pkg/__init__.py": new})

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--old", self.old, "--new", self.new, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    # Exit codes

    def test_exit_0_without_changes(self) -> None:
        self.setup_versions(OLD, OLD)
        code, out, _ = self.run_cli()
        assert code == 0
        assert "Success: no breaking changes found (checked 3 public symbols)" in out

    def test_exit_1_with_breaking_change(self) -> None:
        self.setup_versions()
        code, out, _ = self.run_cli()
        assert code == 1
        assert "[return-widened]" in out
        assert out.splitlines()[-1].startswith(
            "Found 1 breaking change and 0 warnings in 1 symbol"
        )

    def test_warning_fails_only_with_strict(self) -> None:
        self.setup_versions(OLD, NEW_WARNING)
        code, out, _ = self.run_cli()
        assert code == 0, out
        assert "[any-changed]" in out
        code, _, _ = self.run_cli("--strict")
        assert code == 1

    def test_exit_2_on_syntax_error(self) -> None:
        self.setup_versions(OLD, "def read(:\n")
        code, out, err = self.run_cli()
        assert code == 2
        assert "new version failed to build" in err
        assert out == ""

    def test_exit_2_when_package_missing(self) -> None:
        self.setup_versions()
        code, _, err = self.run_cli("-p", "nonexistent")
        assert code == 2
        assert "not found in either version: nonexistent" in err

    def test_exit_2_when_directory_missing(self) -> None:
        code, _, err = self.run_cli()
        assert code == 2
        assert "directory not found" in err

    # JSON

    def test_json_report(self) -> None:
        self.setup_versions()
        code, out, _ = self.run_cli("--format", "json")
        assert code == 1
        data = json.loads(out)
        assert data["schema_version"] == 1
        assert data["old"] == self.old and data["new"] == self.new
        assert data["summary"] == {"breaking": 1, "warning": 0, "info": 0}
        assert data["symbols_checked"] == 3
        [change] = data["changes"]
        assert change["code"] == "return-widened"
        assert change["severity"] == "breaking"
        assert change["symbol"] == "pkg.read"
        assert change["old"] == {
            "path": os.path.join(self.old, "pkg", "__init__.py"),
            "line": 1,
            "signature": "def read(name: str) -> str",
        }
        assert change["new"]["signature"] == "def read(name: str) -> str | None"

    def test_json_includes_info_and_output_file(self) -> None:
        self.setup_versions(OLD, OLD + "def extra() -> None: ...\n")
        report = os.path.join(self.tmp, "report.json")
        code, out, _ = self.run_cli("--format", "json", "-o", report)
        assert code == 0
        assert out == ""
        with open(report, encoding="utf-8") as f:
            data = json.load(f)
        assert [c["code"] for c in data["changes"]] == ["symbol-added"]
        assert data["changes"][0]["old"] is None

    def test_info_hidden_in_text_unless_requested(self) -> None:
        self.setup_versions(OLD, OLD + "def extra() -> None: ...\n")
        _, out, _ = self.run_cli()
        assert "symbol-added" not in out
        _, out, _ = self.run_cli("--show-info")
        assert 'note: Function "pkg.extra" was added  [symbol-added]' in out

    # Allowlists

    def test_allowlist_accepts_changes(self) -> None:
        self.setup_versions()
        allowlist = os.path.join(self.tmp, "allow.txt")
        with open(allowlist, "w", encoding="utf-8") as f:
            f.write("# accepted for 2.0\npkg.read:return-widened\n")
        code, out, _ = self.run_cli("--allowlist", allowlist)
        assert code == 0, out
        assert "return-widened" not in out

    def test_allowlist_code_must_match(self) -> None:
        self.setup_versions()
        allowlist = os.path.join(self.tmp, "allow.txt")
        with open(allowlist, "w", encoding="utf-8") as f:
            f.write("pkg.read:param-renamed\n")
        code, out, _ = self.run_cli("--allowlist", allowlist)
        assert code == 1
        assert "[return-widened]" in out
        assert "Allowlist entry 'pkg.read:param-renamed' didn't match any change" in out

    def test_unused_allowlist_entries(self) -> None:
        self.setup_versions(OLD, OLD)
        allowlist = os.path.join(self.tmp, "allow.txt")
        with open(allowlist, "w", encoding="utf-8") as f:
            f.write("pkg\\.gone\n")
        code, out, _ = self.run_cli("--allowlist", allowlist)
        assert code == 0
        assert "[unused-allowlist]" in out
        code, _, _ = self.run_cli("--allowlist", allowlist, "--strict")
        assert code == 1
        code, out, _ = self.run_cli("--allowlist", allowlist, "--ignore-unused-allowlist")
        assert "unused-allowlist" not in out

    def test_generate_allowlist_round_trip(self) -> None:
        self.setup_versions(OLD, NEW_BREAKING + "def keep2() -> None: ...\n")
        code, out, _ = self.run_cli("--generate-allowlist")
        assert code == 0
        assert "pkg.read:return-widened" in out.splitlines()
        assert "symbol-added" not in out
        allowlist = os.path.join(self.tmp, "allow.txt")
        with open(allowlist, "w", encoding="utf-8") as f:
            f.write(out)
        code, out, _ = self.run_cli("--allowlist", allowlist, "--strict")
        assert code == 0, out

    def test_invalid_allowlist_regex(self) -> None:
        self.setup_versions()
        allowlist = os.path.join(self.tmp, "allow.txt")
        with open(allowlist, "w", encoding="utf-8") as f:
            f.write("pkg.(read\n")
        code, _, err = self.run_cli("--allowlist", allowlist)
        assert code == 2
        assert "invalid regular expression" in err

    # Read-only guarantee

    def tree_digest(self, root: str) -> dict[str, str]:
        result = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                if os.path.isdir(path):
                    result[rel] = "<dir>"
                else:
                    with open(path, "rb") as f:
                        result[rel] = hashlib.sha256(f.read()).hexdigest()
        return result

    def test_trees_are_not_modified(self) -> None:
        self.setup_versions()
        self.write(self.old, {"pkg/sub/mod.py": "class C:\n    x: int\n"})
        self.write(self.new, {"pkg/sub/mod.py": "class C:\n    x: str\n"})
        before = (self.tree_digest(self.old), self.tree_digest(self.new))
        cwd = os.getcwd()
        os.chdir(self.new)
        try:
            code, _, _ = self.run_cli("--format", "json")
        finally:
            os.chdir(cwd)
        assert code == 1
        assert (self.tree_digest(self.old), self.tree_digest(self.new)) == before
        assert not os.path.exists(os.path.join(self.new, ".mypy_cache"))

    def test_refuses_to_write_report_inside_compared_tree(self) -> None:
        self.setup_versions()
        code, _, err = self.run_cli("-o", os.path.join(self.new, "report.txt"))
        assert code == 2
        assert "refusing to write the report inside a compared tree" in err
        assert not os.path.exists(os.path.join(self.new, "report.txt"))

    # Misc

    def test_installed_copy_does_not_shadow_root(self) -> None:
        # "mypy" is importable from the environment running the tests; the version under
        # the root must still win.
        self.write(self.old, {"mypy/__init__.py": "def f() -> int: ...\n"})
        self.write(self.new, {"mypy/__init__.py": "def f() -> str: ...\n"})
        code, out, _ = self.run_cli("-p", "mypy")
        assert code == 1, out
        assert 'Return type of "mypy.f" changed from "int" to "str"' in out

    def test_list_codes(self) -> None:
        code, out, _ = self.run_cli("--list-codes")
        assert code == 0
        lines = out.splitlines()
        assert len(lines) == len(CODES)
        for line in lines:
            assert line.split()[1] in SEVERITIES
