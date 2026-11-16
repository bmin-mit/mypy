"""Data-driven tests for apidiff (mypy.apidiff).

Each test case defines an old and a new version of one or more packages:

    [case testSomething]
    # flags: --show-info
    [file old/pkg/__init__.py]
    def f(x: int) -> int: ...
    [file new/pkg/__init__.py]
    def f(x: int) -> int | None: ...
    [out]
    new/pkg/__init__.py:1: error: Return type of "pkg.f" widened from "int" to "int | None"  [return-widened]

Output lines are the text report without the summary line. Notes (info-level changes)
are included only with "# flags: --show-info". An empty [out] means no changes.
"""

from __future__ import annotations

import os

from mypy.apidiff import compare_trees
from mypy.apidiff.report import render_text
from mypy.test.config import test_temp_dir
from mypy.test.data import DataDrivenTestCase, DataSuite
from mypy.test.helpers import assert_string_arrays_equal


class ApiDiffSuite(DataSuite):
    files = ["apidiff.test"]

    def run_case(self, testcase: DataDrivenTestCase) -> None:
        flags = ""
        for line in testcase.input:
            if line.startswith("# flags:"):
                flags = line[len("# flags:") :]
        show_info = "--show-info" in flags
        old_cwd = os.getcwd()
        os.chdir(test_temp_dir)
        try:
            os.makedirs("old", exist_ok=True)
            os.makedirs("new", exist_ok=True)
            changes, _, _, _ = compare_trees("old", "new")
            output = render_text(changes, fallback_path="new", show_info=show_info)
        finally:
            os.chdir(old_cwd)
        assert_string_arrays_equal(
            testcase.output,
            output.splitlines(),
            f"Invalid output ({testcase.file}, line {testcase.line})",
        )
