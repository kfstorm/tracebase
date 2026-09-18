"""Shared validation for the Summarizer's generic shard protocol."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_STATUS_BLOCK = re.compile(
    r"<!--\s*SHARD_STATUS_BEGIN\s*-->(.*?)<!--\s*SHARD_STATUS_END\s*-->",
    re.DOTALL,
)
_SHARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_USER_WORK_SECTION = "## User work"
_CONTEXT_ONLY_SECTION = "## Context-only evidence"
_PLAN_ARGUMENT_COUNT = 3
_MAX_SHARD_ITEMS = 8
_MAX_SHARD_BYTES = 262144
_SHARD_FIELDS = {"id", "items", "status", "retry_count", "report"}


@dataclass(frozen=True, slots=True)
class ShardObservability:
    """Protocol errors recorded by the shard validator."""

    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CommonValidation:
    errors: tuple[str, ...]


def _status_data(notes: str) -> Any:
    match = _STATUS_BLOCK.search(notes)
    if match is None:
        raise ValueError("NOTES.md has no SHARD_STATUS block")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("NOTES.md SHARD_STATUS block is not valid JSON") from error


def _context_items(index_lines: tuple[str, ...]) -> frozenset[str]:
    """Return exact item roots exposed by overview links in index.md."""
    roots: set[str] = set()
    for line in index_lines:
        for target in re.findall(r"\]\(([^)]+)\)", line):
            if target.endswith("/overview.md"):
                roots.add(target.removesuffix("/overview.md").removeprefix("./"))
    return frozenset(roots)


def _normalized_headings(report: str) -> set[str]:
    headings: set[str] = set()
    fenced = False
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING.match(line)
        if match is None:
            continue
        heading = re.sub(r"\s+#+\s*$", "", match.group(1)).strip()
        headings.add(re.sub(r"\s+", " ", heading).casefold())
    return headings


def _read_context(context_dir: Path) -> tuple[frozenset[str], bool, list[str]]:
    errors: list[str] = []
    try:
        index_lines = tuple(
            (context_dir / "index.md").read_text(encoding="utf-8").splitlines()
        )
    except OSError, UnicodeError:
        return frozenset(), True, ["could not read Context index"]
    return _context_items(index_lines), False, errors


def _context_item_sizes(
    context_dir: Path, item_roots: frozenset[str]
) -> tuple[dict[str, int], list[str]]:
    sizes: dict[str, int] = {}
    errors: list[str] = []
    for root in item_roots:
        item_path = context_dir / root
        if not item_path.is_dir():
            sizes[root] = 0
            continue
        try:
            sizes[root] = sum(
                path.stat().st_size for path in item_path.rglob("*") if path.is_file()
            )
        except OSError:
            errors.append(f"could not measure Context item {root!r}")
    return sizes, errors


def _validate_items(
    shard_id: str,
    value: Any,
    context_items: frozenset[str],
    memberships: dict[str, list[str]],
    *,
    check_membership: bool,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list) or not value:
        return [f"shard {shard_id!r} items must be a non-empty list"]
    if len(value) > _MAX_SHARD_ITEMS:
        errors.append(
            f"shard {shard_id!r} has more than {_MAX_SHARD_ITEMS} Context items"
        )
    local: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            errors.append(f"shard {shard_id!r} contains a malformed item")
            continue
        if item in local:
            errors.append(f"shard {shard_id!r} declares item {item!r} more than once")
            continue
        local.add(item)
        if check_membership and item not in context_items:
            errors.append(f"shard {shard_id!r} contains unknown Context item {item!r}")
        elif check_membership:
            memberships[item].append(shard_id)
    return errors


def _validate_common(
    work_dir: Path, context_dir: Path | None, *, pre_dispatch: bool
) -> _CommonValidation:
    notes_path = work_dir / "NOTES.md"
    try:
        notes = notes_path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return _CommonValidation((f"could not read {notes_path}",))

    try:
        data = _status_data(notes)
    except ValueError as error:
        return _CommonValidation((str(error),))
    if not isinstance(data, dict) or not isinstance(data.get("shards"), list):
        return _CommonValidation(("SHARD_STATUS.shards must be a list",))

    errors: list[str] = []
    context_items: frozenset[str] = frozenset()
    context_sizes: dict[str, int] = {}
    index_error = False
    if context_dir is not None:
        context_items, index_error, context_errors = _read_context(context_dir)
        errors.extend(context_errors)
        if not index_error:
            context_sizes, size_errors = _context_item_sizes(context_dir, context_items)
            errors.extend(size_errors)
        if not index_error and not data["shards"] and context_items:
            errors.append("non-empty Context has no declared shards")

    seen_shard_ids: set[str] = set()
    memberships: dict[str, list[str]] = {item: [] for item in context_items}
    for shard in data["shards"]:
        if not isinstance(shard, dict):
            errors.append("each shard status must be an object")
            continue

        shard_id = shard.get("id")
        if not isinstance(shard_id, str) or _SHARD_ID.fullmatch(shard_id) is None:
            errors.append("shard id is invalid")
            continue
        if shard_id in seen_shard_ids:
            errors.append(f"shard {shard_id!r} is declared more than once")
            continue
        seen_shard_ids.add(shard_id)

        unknown_fields = sorted(set(shard) - _SHARD_FIELDS)
        if unknown_fields:
            errors.append(
                f"shard {shard_id!r} has unknown fields: " + ", ".join(unknown_fields)
            )

        retry_count = shard.get("retry_count")
        status = shard.get("status")
        if pre_dispatch:
            if status != "pending":
                errors.append(f"shard {shard_id!r} is not pending")
            if (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count != 0
            ):
                errors.append(
                    f"shard {shard_id!r} retry_count must be 0 before dispatch"
                )
        else:
            if not isinstance(retry_count, int) or isinstance(retry_count, bool):
                errors.append(f"shard {shard_id!r} retry_count is invalid")
            elif retry_count < 0 or retry_count > 1:
                errors.append(f"shard {shard_id!r} was retried more than once")
            if status != "complete":
                errors.append(f"shard {shard_id!r} is not in a terminal state")

        if context_dir is not None and not index_error:
            errors.extend(
                _validate_items(
                    shard_id,
                    shard.get("items"),
                    context_items,
                    memberships,
                    check_membership=True,
                )
            )
        else:
            errors.extend(
                _validate_items(
                    shard_id,
                    shard.get("items"),
                    context_items,
                    memberships,
                    check_membership=False,
                )
            )

        if context_dir is not None and not index_error:
            declared_items = shard.get("items")
            if isinstance(declared_items, list) and all(
                isinstance(item, str) and item in context_items
                for item in declared_items
            ):
                shard_bytes = sum(context_sizes[item] for item in declared_items)
                oversized_item = (
                    len(declared_items) == 1
                    and context_sizes[declared_items[0]] > _MAX_SHARD_BYTES
                )
                if shard_bytes > _MAX_SHARD_BYTES and not oversized_item:
                    errors.append(
                        f"shard {shard_id!r} exceeds {_MAX_SHARD_BYTES} readable bytes"
                    )

        expected_report = f"/work/shards/{shard_id}.md"
        if shard.get("report") != expected_report:
            errors.append(f"shard {shard_id!r} has a non-canonical report path")

        if not pre_dispatch and shard.get("report") == expected_report:
            report_path = work_dir / "shards" / f"{shard_id}.md"
            if report_path.is_symlink() or not report_path.is_file():
                errors.append(f"shard {shard_id!r} canonical report is missing")
            else:
                try:
                    report_text = report_path.read_text(encoding="utf-8")
                    if not report_text.strip():
                        errors.append(f"shard {shard_id!r} canonical report is empty")
                    elif not {
                        _USER_WORK_SECTION.casefold()[3:],
                        _CONTEXT_ONLY_SECTION.casefold()[3:],
                    }.issubset(_normalized_headings(report_text)):
                        errors.append(
                            f"shard {shard_id!r} report does not separate user work "
                            "from context-only evidence"
                        )
                except OSError, UnicodeError:
                    errors.append(f"shard {shard_id!r} canonical report cannot be read")

    if context_dir is not None and not index_error:
        for item, owners in sorted(memberships.items()):
            if not owners:
                errors.append(f"Context item {item!r} is not covered by any shard")
            elif len(owners) > 1:
                errors.append(
                    f"Context item {item!r} is covered by multiple shards: "
                    + ", ".join(owners)
                )
    return _CommonValidation(tuple(errors))


def inspect_shard_plan(work_dir: Path, context_dir: Path) -> ShardObservability:
    """Validate the complete, untouched shard inventory before dispatch."""
    return ShardObservability(
        _validate_common(work_dir, context_dir, pre_dispatch=True).errors
    )


def inspect_shards(
    work_dir: Path, context_dir: Path | None = None
) -> ShardObservability:
    """Validate terminal shard state, coverage, and non-empty reports."""
    return ShardObservability(
        _validate_common(work_dir, context_dir, pre_dispatch=False).errors
    )


def main(arguments: list[str] | None = None) -> int:
    """Run the pre-dispatch validator without importing Tracebase."""
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != _PLAN_ARGUMENT_COUNT or args[0] != "plan":
        return 2
    result = inspect_shard_plan(Path(args[1]), Path(args[2]))
    if result.errors:
        for error in result.errors:
            print(error)
        return 1
    print("Shard plan validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
