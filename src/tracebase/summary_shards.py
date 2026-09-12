"""Validation and observability for the root Summarizer's shard protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_STATUS_BLOCK = re.compile(
    r"<!--\s*SHARD_STATUS_BEGIN\s*-->(.*?)<!--\s*SHARD_STATUS_END\s*-->",
    re.DOTALL,
)
_SHARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class ShardObservability:
    """Terminal shard counts and protocol errors recorded by the harness."""

    errors: tuple[str, ...]


def _status_data(notes: str) -> Any:
    match = _STATUS_BLOCK.search(notes)
    if match is None:
        raise ValueError("NOTES.md has no SHARD_STATUS block")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("NOTES.md SHARD_STATUS block is not valid JSON") from error


def inspect_shards(
    work_dir: Path, context_dir: Path | None = None
) -> ShardObservability:
    """Check every declared shard has one non-empty canonical report."""
    notes_path = work_dir / "NOTES.md"
    try:
        notes = notes_path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return ShardObservability((f"could not read {notes_path}",))

    try:
        data = _status_data(notes)
    except ValueError as error:
        return ShardObservability((str(error),))
    if not isinstance(data, dict) or not isinstance(data.get("shards"), list):
        return ShardObservability(("SHARD_STATUS.shards must be a list",))
    if context_dir is not None and not data["shards"]:
        try:
            has_context = any(path.is_file() for path in context_dir.rglob("*"))
        except OSError:
            has_context = True
        if has_context:
            return ShardObservability(("non-empty Context has no declared shards",))

    errors: list[str] = []
    seen: set[str] = set()
    for item in data["shards"]:
        if not isinstance(item, dict):
            errors.append("each shard status must be an object")
            continue
        shard_id = item.get("id")
        status = item.get("status")
        retry_count = item.get("retry_count", 0)
        report = item.get("report")
        if not isinstance(shard_id, str) or _SHARD_ID.fullmatch(shard_id) is None:
            errors.append("shard id is invalid")
            continue
        if shard_id in seen:
            errors.append(f"shard {shard_id!r} is declared more than once")
            continue
        seen.add(shard_id)
        if not isinstance(retry_count, int) or isinstance(retry_count, bool):
            errors.append(f"shard {shard_id!r} retry_count is invalid")
        elif retry_count < 0 or retry_count > 1:
            errors.append(f"shard {shard_id!r} was retried more than once")
        if status == "failed":
            errors.append(f"shard {shard_id!r} reported failure")
        elif status != "complete":
            errors.append(f"shard {shard_id!r} is not in a terminal state")

        expected_report = f"/work/shards/{shard_id}.md"
        if report != expected_report:
            errors.append(f"shard {shard_id!r} has a non-canonical report path")
        else:
            report_path = work_dir / "shards" / f"{shard_id}.md"
            if report_path.is_symlink() or not report_path.is_file():
                errors.append(f"shard {shard_id!r} canonical report is missing")
            else:
                try:
                    if not report_path.read_text(encoding="utf-8").strip():
                        errors.append(f"shard {shard_id!r} canonical report is empty")
                except OSError, UnicodeError:
                    errors.append(f"shard {shard_id!r} canonical report cannot be read")
    return ShardObservability(tuple(errors))
