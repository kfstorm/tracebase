"""Deterministic OpenCode Context Output projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .archive import ArchiveError, PublishedSnapshot


@dataclass(frozen=True, slots=True)
class OpenCodeProjection:
    """The OpenCode-specific, serializable view of one session."""

    selected: bool
    inclusion_reasons: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    session: dict[str, Any]
    messages: tuple[dict[str, Any], ...]
    task_children: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, str], ...]

    @property
    def session_id(self) -> str:
        info = self.session.get("info")
        identifier = (
            info.get("id") if isinstance(info, dict) else self.session.get("id")
        )
        if not isinstance(identifier, str):
            raise ArchiveError("OpenCode session payload is invalid")
        return identifier

    @property
    def parent_id(self) -> str | None:
        info = self.session.get("info")
        parent = (
            info.get("parentID")
            if isinstance(info, dict)
            else self.session.get("parentID")
        )
        return parent if isinstance(parent, str) else None

    @property
    def explicit_task_child_ids(self) -> tuple[str, ...]:
        result: list[str] = []
        for part in self.task_children:
            state = part.get("state")
            metadata = state.get("metadata") if isinstance(state, dict) else None
            child_id = metadata.get("sessionId") if isinstance(metadata, dict) else None
            parent_id = (
                metadata.get("parentSessionId") if isinstance(metadata, dict) else None
            )
            if (
                isinstance(child_id, str)
                and isinstance(parent_id, str)
                and parent_id == self.session_id
            ):
                result.append(child_id)
        return tuple(dict.fromkeys(result))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except OverflowError, OSError, ValueError:
            raise ArchiveError("OpenCode context timestamp is invalid") from None
    if not isinstance(value, str):
        raise ArchiveError("OpenCode context timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArchiveError("OpenCode context timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveError("OpenCode context timestamp is invalid")
    return parsed


def _json(snapshot: PublishedSnapshot) -> dict[str, Any]:
    try:
        value = json.loads(snapshot.evidence["session.json"])
    except KeyError, UnicodeDecodeError, json.JSONDecodeError:
        raise ArchiveError("OpenCode session payload is invalid") from None
    if not isinstance(value, dict):
        raise ArchiveError("OpenCode session payload is invalid")
    return value


def _created(value: dict[str, Any]) -> datetime:
    # OpenCode has used both flattened exports and info.time payloads.
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    time_data = info.get("time", value.get("time"))
    time_data = time_data if isinstance(time_data, dict) else {}
    candidates = (value.get("created"), time_data.get("created"))
    for candidate in candidates:
        if isinstance(candidate, (int, float, str)) and not isinstance(candidate, bool):
            return _timestamp(candidate)
    raise ArchiveError("OpenCode message payload is invalid")


def _roles(times: list[datetime], start: datetime, end: datetime) -> tuple[str, ...]:
    if not times:
        return ("observed_state",)
    return tuple(
        role
        for role, present in (
            ("in_range_work", any(start <= value < end for value in times)),
            ("earlier_background", any(value < start for value in times)),
            ("later_progression", any(value >= end for value in times)),
        )
        if present
    )


def _tool_times(part: dict[str, Any]) -> tuple[datetime, datetime] | None:
    state = part.get("state")
    if not isinstance(state, dict):
        return None
    time_data = state.get("time")
    time_data = time_data if isinstance(time_data, dict) else {}
    start = time_data.get("start", state.get("started", state.get("start")))
    end = time_data.get(
        "end", state.get("ended", state.get("end", state.get("completed")))
    )
    if not isinstance(start, (int, float, str)) or isinstance(start, bool):
        raise ArchiveError("OpenCode tool payload is invalid")
    started = _timestamp(start)
    if end is None:
        return (started, started)
    if not isinstance(end, (int, float, str)) or isinstance(end, bool):
        raise ArchiveError("OpenCode tool payload is invalid")
    ended = _timestamp(end)
    if ended < started:
        raise ArchiveError("OpenCode tool payload is invalid")
    return started, ended


def project_opencode(
    snapshots: tuple[PublishedSnapshot, ...], start: datetime, end: datetime
) -> OpenCodeProjection:
    """Project messages as points and tool executions as intervals."""
    if not snapshots:
        raise ArchiveError("OpenCode session has no snapshot")
    session = _json(snapshots[-1])
    messages: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    for snapshot in snapshots:
        payload = _json(snapshot)
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise ArchiveError("OpenCode session payload is invalid")
        for message in raw_messages:
            if not isinstance(message, dict):
                raise ArchiveError("OpenCode message payload is invalid")
            info = message.get("info")
            info = info if isinstance(info, dict) else {}
            message_id = message.get("id", info.get("id"))
            if not isinstance(message_id, str) or not message_id:
                raise ArchiveError("OpenCode message payload is invalid")
            message_time = _created(message)
            record: dict[str, Any] = {
                "id": message_id,
                "created": message_time.isoformat(),
                "role": message.get("role", info.get("role")),
                "value": message,
                "temporal_roles": _roles([message_time], start, end),
            }
            parts = message.get("parts", ())
            if not isinstance(parts, list):
                raise ArchiveError("OpenCode message payload is invalid")
            tools: list[dict[str, Any]] = []
            for part in parts:
                if not isinstance(part, dict):
                    raise ArchiveError("OpenCode message payload is invalid")
                if part.get("type") == "tool":
                    interval = _tool_times(part)
                    if interval is not None:
                        tool_start, tool_end = interval
                        tools.append(
                            {
                                "value": part,
                                "start": tool_start.isoformat(),
                                "end": tool_end.isoformat(),
                                "temporal_roles": _roles(
                                    [tool_start, tool_end], start, end
                                ),
                            }
                        )
                if part.get("type") == "task" or part.get("tool") == "task":
                    children.append(part)
            if tools:
                record["tools"] = tools
            messages.append(record)
    if not any("in_range_work" in record["temporal_roles"] for record in messages):
        gaps.append({"kind": "no_in_range_messages", "reason": "bounded_range"})
    all_roles = tuple(
        sorted(
            {role for message in messages for role in message["temporal_roles"]}
            | {
                role
                for message in messages
                for tool in message.get("tools", ())
                for role in tool["temporal_roles"]
            }
        )
    )
    selected = any("in_range_work" in message["temporal_roles"] for message in messages)
    return OpenCodeProjection(
        selected,
        ("in_range_source_record",) if selected else (),
        all_roles,
        session,
        tuple(messages),
        tuple(children),
        tuple(gaps),
    )
