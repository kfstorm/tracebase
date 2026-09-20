"""Shared validation for the Summarizer's generic shard protocol."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tracebase.context_inventory import (  # type: ignore[import-untyped]
        ContextInventory,
        ContextInventoryError,
        item_readable_sizes,
        load_context_inventory,
    )
    from tracebase.summary_planner import (  # type: ignore[import-untyped]
        SHARD_POLICY,
        PlannedShard,
        plan_shards,
        shard_task_text,
    )
else:
    from .context_inventory import (
        ContextInventory,
        ContextInventoryError,
        item_readable_sizes,
        load_context_inventory,
    )
    from .summary_planner import (
        SHARD_POLICY,
        PlannedShard,
        plan_shards,
        shard_task_text,
    )

_SHARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_USER_WORK_SECTION = "## User work"
_CONTEXT_ONLY_SECTION = "## Context-only evidence"
_PLAN_ARGUMENT_COUNT = 3
_REPORT_HEADING_COUNT = 2
_STATUS_FIELDS = {"status", "retry_count"}


@dataclass(frozen=True, slots=True)
class ShardObservability:
    """Protocol errors recorded by the shard validator."""

    errors: tuple[str, ...]


def _normalized_headings(report: str) -> tuple[str, ...]:
    headings: list[str] = []
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
        headings.append(re.sub(r"\s+", " ", heading).casefold())
    return tuple(headings)


def _read_context(
    context_dir: Path,
) -> tuple[ContextInventory | None, bool, list[str]]:
    try:
        return load_context_inventory(context_dir), False, []
    except ContextInventoryError as error:
        return None, True, [str(error)]


def _read_status(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return None, [f"could not read valid status from {path}"]
    if not isinstance(raw, dict) or set(raw) != _STATUS_FIELDS:
        return None, [f"{path} must contain only status and retry_count"]
    return raw, []


def _assigned_paths(task: str) -> tuple[str, ...]:
    lines = task.splitlines()
    try:
        start = lines.index("## Assigned evidence") + 1
        end = lines.index("## Worker contract", start)
    except ValueError:
        return ()
    return tuple(
        line[2:] for line in lines[start:end] if line.startswith("- /context/")
    )


def _items_from_paths(
    paths: tuple[str, ...], inventory: ContextInventory
) -> tuple[tuple[str, ...], list[str]]:
    errors: list[str] = []
    files_by_root = {item.root: set(item.files) for item in inventory.items}
    assigned: dict[str, list[str]] = {}
    for path in paths:
        relative = path.removeprefix("/context/")
        matches = [
            root
            for root, files in files_by_root.items()
            if relative.startswith(root + "/")
            and relative.removeprefix(root + "/") in files
        ]
        if len(matches) != 1:
            errors.append(f"task assigns unknown Context file {path!r}")
            continue
        root = matches[0]
        file = relative.removeprefix(root + "/")
        if file in assigned.setdefault(root, []):
            errors.append(f"task assigns Context file {path!r} more than once")
        else:
            assigned[root].append(file)
    for item in inventory.items:
        files = assigned.get(item.root)
        if files is not None and tuple(files) != item.files:
            errors.append(f"task does not assign the exact files for {item.root!r}")
    return tuple(
        root for root in (item.root for item in inventory.items) if root in assigned
    ), errors


def _validate_report(work_dir: Path, shard_id: str) -> list[str]:
    report_path = work_dir / "shards" / shard_id / "REPORT.md"
    if report_path.is_symlink() or not report_path.is_file():
        return [f"shard {shard_id!r} report is missing"]
    try:
        report = report_path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return [f"shard {shard_id!r} report cannot be read"]
    if not report.strip():
        return [f"shard {shard_id!r} report is empty"]
    required = {
        _USER_WORK_SECTION.casefold()[3:],
        _CONTEXT_ONLY_SECTION.casefold()[3:],
    }
    headings = _normalized_headings(report)
    if len(headings) != _REPORT_HEADING_COUNT or set(headings) != required:
        return [
            f"shard {shard_id!r} report does not separate user work from "
            "context-only evidence"
        ]
    return []


def _validate_common(
    work_dir: Path,
    context_dir: Path | None,
    *,
    pre_dispatch: bool,
    expected_plan: tuple[PlannedShard, ...] | None = None,
) -> tuple[str, ...]:
    errors: list[str] = []
    inventory: ContextInventory | None = None
    context_items: frozenset[str] = frozenset()
    context_sizes: dict[str, int] = {}
    if context_dir is not None:
        inventory, index_error, context_errors = _read_context(context_dir)
        errors.extend(context_errors)
        if not index_error:
            assert inventory is not None
            context_items = frozenset(item.root for item in inventory.items)
            try:
                context_sizes = item_readable_sizes(context_dir, inventory)
            except ContextInventoryError as error:
                errors.append(str(error))

    shards_dir = work_dir / "shards"
    if shards_dir.is_symlink() or not shards_dir.is_dir():
        return (*errors, "work shards directory is missing")
    shard_entries = tuple(shards_dir.iterdir())
    shard_dirs = sorted(path for path in shard_entries if path.is_dir())
    if any(not path.is_dir() for path in shard_entries):
        errors.append("work shards directory contains a non-directory entry")
    if any(path.is_symlink() for path in shard_dirs):
        errors.append("shard directory must not be a symlink")
    expected_by_id = {shard.id: shard for shard in expected_plan or ()}
    actual_ids: list[str] = []
    memberships: dict[str, list[str]] = {item: [] for item in context_items}
    for shard_dir in shard_dirs:
        shard_id = shard_dir.name
        actual_ids.append(shard_id)
        if _SHARD_ID.fullmatch(shard_id) is None:
            errors.append(f"shard id {shard_id!r} is invalid")
            continue
        task_path = shard_dir / "TASK.md"
        status_path = shard_dir / "STATUS.json"
        required_files = {"TASK.md", "STATUS.json"}
        if not pre_dispatch:
            required_files.add("REPORT.md") if (
                shard_dir / "REPORT.md"
            ).exists() else None
        actual_files = {path.name for path in shard_dir.iterdir()}
        if not required_files.issuperset(actual_files) or not {
            "TASK.md",
            "STATUS.json",
        }.issubset(actual_files):
            errors.append(f"shard {shard_id!r} has an invalid package layout")
        if task_path.is_symlink() or not task_path.is_file():
            errors.append(f"shard {shard_id!r} task is not a regular file")
        if status_path.is_symlink() or not status_path.is_file():
            errors.append(f"shard {shard_id!r} status is not a regular file")
        try:
            task = task_path.read_text(encoding="utf-8")
        except OSError, UnicodeError:
            task = ""
            errors.append(f"shard {shard_id!r} task is missing or unreadable")
        status, status_errors = _read_status(status_path)
        errors.extend(status_errors)
        if status is not None:
            state = status.get("status")
            retry_count = status.get("retry_count")
            if pre_dispatch:
                if state != "pending":
                    errors.append(f"shard {shard_id!r} is not pending")
                if (
                    not isinstance(retry_count, int)
                    or isinstance(retry_count, bool)
                    or retry_count != 0
                ):
                    errors.append(
                        f"shard {shard_id!r} retry_count must be 0 before dispatch"
                    )
            elif state not in {"complete", "failed"}:
                errors.append(f"shard {shard_id!r} is not in a terminal state")
            elif (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count not in {0, 1}
            ):
                errors.append(f"shard {shard_id!r} retry_count is invalid")
            elif state == "failed" and retry_count != 1:
                errors.append(f"shard {shard_id!r} failed without exhausting its retry")
            elif state == "failed":
                errors.append(f"shard {shard_id!r} reported failure")
            elif state == "complete":
                errors.extend(_validate_report(work_dir, shard_id))

        assigned_items: tuple[str, ...] = ()
        if inventory is not None:
            assigned_items, task_errors = _items_from_paths(
                _assigned_paths(task), inventory
            )
            errors.extend(f"shard {shard_id!r}: {error}" for error in task_errors)
            for item in assigned_items:
                memberships[item].append(shard_id)
            if all(item in context_sizes for item in assigned_items):
                shard_bytes = sum(context_sizes[item] for item in assigned_items)
                oversized_item = (
                    len(assigned_items) == 1
                    and context_sizes[assigned_items[0]] > SHARD_POLICY.max_bytes
                )
                if shard_bytes > SHARD_POLICY.max_bytes and not oversized_item:
                    errors.append(
                        f"shard {shard_id!r} exceeds {SHARD_POLICY.max_bytes} "
                        "readable bytes"
                    )
            if len(assigned_items) > SHARD_POLICY.max_items:
                errors.append(
                    f"shard {shard_id!r} has more than {SHARD_POLICY.max_items} "
                    "Context items"
                )

        expected = expected_by_id.get(shard_id)
        if expected is not None:
            if assigned_items != expected.items:
                errors.append(f"shard {shard_id!r} changed its host-assigned item list")
            if inventory is not None:
                expected_task = shard_task_text(expected, inventory)
                if task != expected_task:
                    errors.append(
                        f"shard {shard_id!r} task does not match the host plan"
                    )
        elif expected_plan is not None:
            errors.append(f"shard {shard_id!r} was not in the host-generated plan")

        if pre_dispatch and (shard_dir / "REPORT.md").exists():
            errors.append(f"shard {shard_id!r} has a pre-created report")

    if inventory is not None:
        for item, owners in sorted(memberships.items()):
            if not owners:
                errors.append(f"Context item {item!r} is not covered by any shard")
            elif len(owners) > 1:
                errors.append(
                    f"Context item {item!r} is covered by multiple shards: "
                    + ", ".join(owners)
                )
    if expected_plan is not None:
        expected_ids = tuple(shard.id for shard in expected_plan)
        if set(actual_ids) != set(expected_ids):
            errors.append("shard package IDs differ from the host-generated plan")
    return tuple(errors)


def inspect_shard_plan(
    work_dir: Path,
    context_dir: Path,
    *,
    expected_plan: tuple[PlannedShard, ...] | None = None,
) -> ShardObservability:
    """Validate the complete, untouched shard packages before dispatch."""
    if expected_plan is None:
        try:
            expected_plan = plan_shards(context_dir)
        except ValueError as error:
            return ShardObservability((str(error),))
    return ShardObservability(
        _validate_common(
            work_dir, context_dir, pre_dispatch=True, expected_plan=expected_plan
        )
    )


def inspect_shards(
    work_dir: Path,
    context_dir: Path | None = None,
    *,
    expected_plan: tuple[PlannedShard, ...] | None = None,
) -> ShardObservability:
    """Validate terminal shard state, coverage, and non-empty reports."""
    return ShardObservability(
        _validate_common(
            work_dir,
            context_dir,
            pre_dispatch=False,
            expected_plan=expected_plan,
        )
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
