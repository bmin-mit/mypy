"""Render comparison results as mypy-style text or JSON."""

from __future__ import annotations

import json
from typing import Any, Final

from mypy.apidiff.compare import BREAKING, INFO, WARNING, Change, Location

JSON_SCHEMA_VERSION: Final = 1

TEXT_SEVERITY: Final = {BREAKING: "error", WARNING: "warning", INFO: "note"}


def sort_key(change: Change) -> tuple[str, int, str]:
    # Changes to the same symbol keep the order in which they were found (e.g. parameters
    # from left to right).
    loc = change.new or change.old
    if loc is None:
        return ("~", 0, change.symbol)
    return (loc.path, loc.line, change.symbol)


def sorted_changes(changes: list[Change]) -> list[Change]:
    return sorted(changes, key=sort_key)


def count(changes: list[Change]) -> dict[str, int]:
    result = {BREAKING: 0, WARNING: 0, INFO: 0}
    for change in changes:
        result[change.severity] += 1
    return result


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def format_text_line(change: Change, fallback_path: str) -> str:
    loc = change.new or change.old
    where = f"{loc.path}:{loc.line}" if loc is not None else fallback_path
    return f"{where}: {TEXT_SEVERITY[change.severity]}: {change.message}  [{change.code}]"


def summary_line(changes: list[Change], symbols_checked: int) -> str:
    counts = count(changes)
    checked = f"checked {plural(symbols_checked, 'public symbol')}"
    if not counts[BREAKING] and not counts[WARNING]:
        return f"Success: no breaking changes found ({checked})"
    symbols = len({c.symbol for c in changes if c.severity in (BREAKING, WARNING)})
    return (
        f"Found {plural(counts[BREAKING], 'breaking change')} and "
        f"{plural(counts[WARNING], 'warning')} in {plural(symbols, 'symbol')} ({checked})"
    )


def render_text(
    changes: list[Change],
    *,
    fallback_path: str,
    show_info: bool = False,
    symbols_checked: int | None = None,
) -> str:
    lines = [
        format_text_line(change, fallback_path)
        for change in sorted_changes(changes)
        if show_info or change.severity != INFO
    ]
    if symbols_checked is not None:
        lines.append(summary_line(changes, symbols_checked))
    return "\n".join(lines) + ("\n" if lines else "")


def location_json(loc: Location | None) -> dict[str, Any] | None:
    if loc is None:
        return None
    return {"path": loc.path, "line": loc.line, "signature": loc.signature}


def render_json(
    changes: list[Change], *, old_root: str, new_root: str, symbols_checked: int
) -> str:
    data = {
        "schema_version": JSON_SCHEMA_VERSION,
        "old": old_root,
        "new": new_root,
        "symbols_checked": symbols_checked,
        "summary": count(changes),
        "changes": [
            {
                "code": c.code,
                "severity": c.severity,
                "symbol": c.symbol,
                "message": c.message,
                "old": location_json(c.old),
                "new": location_json(c.new),
            }
            for c in sorted_changes(changes)
        ],
    }
    return json.dumps(data, indent=2) + "\n"
