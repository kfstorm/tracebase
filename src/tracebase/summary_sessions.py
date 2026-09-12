"""OpenCode event parsing and persisted session-tree handling."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


class SessionError(ValueError):
    """Raised when OpenCode session data cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    parent_id: str | None
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_reasoning: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    cost: float = 0.0


def iter_parts(export: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield typed parts from every well-formed message in an export."""
    messages = export.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        yield from (part for part in parts if isinstance(part, dict))


def parse_jsonl(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise SessionError("OpenCode run output is not valid JSONL") from error
        if isinstance(value, dict):
            events.append(value)
    return events


def root_session_id(output: str) -> str:
    """Use the first persisted session identifier emitted by `opencode run`."""
    for event in parse_jsonl(output):
        for key in ("sessionID", "session_id", "sessionId"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        part = event.get("part")
        if isinstance(part, dict):
            for key in ("sessionID", "session_id", "sessionId"):
                value = part.get(key)
                if isinstance(value, str) and value:
                    return value
    raise SessionError("OpenCode run did not emit a root session ID")


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: Any) -> float:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


def parse_session_rows(value: Any) -> dict[str, SessionRecord]:
    if not isinstance(value, list):
        raise SessionError("OpenCode database query did not return a list")
    records: dict[str, SessionRecord] = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise SessionError("OpenCode database session row is invalid")
        parent_id = row.get("parent_id")
        records[row["id"]] = SessionRecord(
            session_id=row["id"],
            parent_id=parent_id if isinstance(parent_id, str) else None,
            tokens_input=_integer(row.get("tokens_input")),
            tokens_output=_integer(row.get("tokens_output")),
            tokens_reasoning=_integer(row.get("tokens_reasoning")),
            tokens_cache_read=_integer(row.get("tokens_cache_read")),
            tokens_cache_write=_integer(row.get("tokens_cache_write")),
            cost=_float(row.get("cost")),
        )
    return records


def descendant_session_ids(
    root_id: str, records: dict[str, SessionRecord]
) -> tuple[str, ...]:
    """Return root plus every reachable child once, in stable order."""
    if root_id not in records:
        raise SessionError("root session is absent from the OpenCode database")
    children: dict[str, list[str]] = {}
    for record in records.values():
        if record.parent_id is not None:
            children.setdefault(record.parent_id, []).append(record.session_id)
    for values in children.values():
        values.sort()
    result: list[str] = []
    seen: set[str] = set()
    pending = [root_id]
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        pending[0:0] = children.get(current, [])
    return tuple(result)


def _export_compaction_count(export: dict[str, Any]) -> int:
    count = 0
    messages = export.get("messages")
    if not isinstance(messages, list):
        return count
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        count += sum(
            isinstance(part, dict) and part.get("type") == "compaction"
            for part in parts
        )
    return count


def compaction_count(exports: dict[str, dict[str, Any]]) -> int:
    """Count explicit compaction parts across the root session tree."""
    return sum(_export_compaction_count(export) for export in exports.values())


def root_compaction_count(root_id: str, exports: dict[str, dict[str, Any]]) -> int:
    """Count explicit compaction parts in the root session only."""
    return _export_compaction_count(exports.get(root_id, {}))
