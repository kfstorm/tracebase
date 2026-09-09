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
    def parent_value(self) -> Any:
        info = self.session.get("info")
        return (
            info.get("parentID")
            if isinstance(info, dict)
            else self.session.get("parentID")
        )

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


def _supporting_message(message: dict[str, Any], parts: list[dict[str, Any]]) -> bool:
    info = message.get("info")
    if isinstance(info, dict) and (
        info.get("mode") == "compaction"
        or info.get("summary") is True
        or info.get("agent") == "compaction"
    ):
        return True
    return any(
        part.get("synthetic") is True
        or (
            isinstance(part.get("metadata"), dict)
            and part["metadata"].get("compaction_continue") is True
        )
        for part in parts
    )


def _point_roles(value: datetime, start: datetime, end: datetime) -> tuple[str, ...]:
    return tuple(
        role
        for role, present in (
            ("in_range_work", start <= value < end),
            ("earlier_background", value < start),
            ("later_progression", value >= end),
        )
        if present
    )


def _interval_roles(
    interval: tuple[datetime, datetime | None], start: datetime, end: datetime
) -> tuple[str, ...]:
    began, finished = interval
    if finished == began:
        return _point_roles(began, start, end)
    in_range = (
        start <= began < end if finished is None else began < end and start < finished
    )
    return tuple(
        role
        for role, present in (
            ("in_range_work", in_range),
            ("earlier_background", finished is not None and finished <= start),
            ("later_progression", began >= end),
        )
        if present
    )


def _interval_qualifies(
    interval: tuple[datetime, datetime | None], start: datetime, end: datetime
) -> bool:
    began, finished = interval
    if finished == began or finished is None:
        return start <= began < end
    return began < end and start < finished


def _refresh_tool_roles(tool: dict[str, Any], start: datetime, end: datetime) -> None:
    if not tool["intervals"]:
        tool["temporal_roles"] = ("observed_state",)
        return
    roles: set[str] = set()
    for interval in tool["intervals"]:
        roles.update(_interval_roles(interval, start, end))
    tool["temporal_roles"] = tuple(sorted(roles))


def _tool_times(part: dict[str, Any]) -> tuple[datetime, datetime | None] | None:
    state = part.get("state")
    if not isinstance(state, dict):
        return None
    time_data = state.get("time")
    time_data = time_data if isinstance(time_data, dict) else {}
    start = time_data.get("start", state.get("started", state.get("start")))
    end = time_data.get(
        "end", state.get("ended", state.get("end", state.get("completed")))
    )
    if start is None and state.get("status") == "pending":
        return None
    if not isinstance(start, (int, float, str)) or isinstance(start, bool):
        raise ArchiveError("OpenCode tool payload is invalid")
    started = _timestamp(start)
    if end is None:
        return (started, None)
    if not isinstance(end, (int, float, str)) or isinstance(end, bool):
        raise ArchiveError("OpenCode tool payload is invalid")
    ended = _timestamp(end)
    if ended < started:
        raise ArchiveError("OpenCode tool payload is invalid")
    return started, ended


def project_opencode(  # noqa: PLR0915
    snapshots: tuple[PublishedSnapshot, ...], start: datetime, end: datetime
) -> OpenCodeProjection:
    """Project messages as points and tool executions as intervals."""
    if not snapshots:
        raise ArchiveError("OpenCode session has no snapshot")
    session = _json(snapshots[-1])
    messages_by_id: dict[str, dict[str, Any]] = {}
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
                "temporal_roles": _point_roles(message_time, start, end),
                "representations": [
                    {
                        "run_id": snapshot.run["run_id"],
                        "observation_window": snapshot.manifest["observation_window"],
                        "value": message,
                    }
                ],
            }
            parts = message.get("parts", ())
            if not isinstance(parts, list):
                raise ArchiveError("OpenCode message payload is invalid")
            if not all(isinstance(part, dict) for part in parts):
                raise ArchiveError("OpenCode message payload is invalid")
            supporting_message = _supporting_message(message, parts)
            record["_created_time"] = message_time
            record["_supporting"] = supporting_message
            if supporting_message:
                record["temporal_roles"] = ("observed_state",)
            tools: dict[str, dict[str, Any]] = {}
            for part in parts:
                if not isinstance(part, dict):
                    raise ArchiveError("OpenCode message payload is invalid")
                if part.get("type") == "tool":
                    interval = _tool_times(part)
                    tool_id = part.get("id", part.get("callID"))
                    if not isinstance(tool_id, str) or not tool_id:
                        tool_id = json.dumps(
                            part, sort_keys=True, separators=(",", ":")
                        )
                    tool = tools.setdefault(
                        tool_id,
                        {
                            "id": tool_id,
                            "value": part,
                            "temporal_roles": (
                                _interval_roles(interval, start, end)
                                if interval is not None
                                else ("observed_state",)
                            ),
                            "representations": [],
                            "intervals": [],
                        },
                    )
                    tool["representations"].append(
                        {
                            "run_id": snapshot.run["run_id"],
                            "observation_window": snapshot.manifest[
                                "observation_window"
                            ],
                            "value": part,
                        }
                    )
                    if interval is not None:
                        tool["intervals"].append(interval)
                        tool_start, tool_end = interval
                        tool["_start_time"] = tool_start
                        tool["start"] = tool_start.isoformat()
                        if tool_end is not None:
                            tool["end"] = tool_end.isoformat()
                if part.get("type") == "task" or part.get("tool") == "task":
                    task_interval = _tool_times(part)
                    if task_interval is not None and _interval_qualifies(
                        task_interval, start, end
                    ):
                        children.append(part)
            if tools:
                record["tools"] = list(tools.values())
            prior = messages_by_id.get(message_id)
            if prior is None:
                messages_by_id[message_id] = record
            else:
                prior["representations"].extend(record["representations"])
                prior["_supporting"] = prior["_supporting"] or record["_supporting"]
                if prior["_supporting"]:
                    prior["temporal_roles"] = ("observed_state",)
                prior_tools = {tool["id"]: tool for tool in prior.get("tools", ())}
                for tool in record.get("tools", ()):
                    prior_tool = prior_tools.get(tool["id"])
                    if prior_tool is None:
                        prior.setdefault("tools", []).append(tool)
                    else:
                        prior_tool["representations"].extend(tool["representations"])
                        if "_start_time" in tool:
                            prior_tool["_start_time"] = tool["_start_time"]
                            prior_tool["start"] = tool["start"]
                        if "end" in tool:
                            prior_tool["end"] = tool["end"]
                        prior_tool["intervals"].extend(tool["intervals"])
                        _refresh_tool_roles(prior_tool, start, end)
    messages = list(messages_by_id.values())
    messages.sort(key=lambda message: (message["_created_time"], message["id"]))
    for message in messages:
        message.get("tools", []).sort(
            key=lambda tool: (
                "_start_time" not in tool,
                tool.get("_start_time"),
                tool["id"],
            )
        )
        for tool in message.get("tools", ()):
            _refresh_tool_roles(tool, start, end)
            tool.pop("intervals", None)
            tool.pop("_start_time", None)
        message.pop("_created_time", None)
    children = list(
        {
            json.dumps(child, sort_keys=True, separators=(",", ":")): child
            for child in children
        }.values()
    )
    if not any(
        (not record["_supporting"] and "in_range_work" in record["temporal_roles"])
        or any(
            "in_range_work" in tool["temporal_roles"]
            for tool in record.get("tools", ())
        )
        for record in messages
    ):
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
    selected = any(
        (not message["_supporting"] and "in_range_work" in message["temporal_roles"])
        or any(
            "in_range_work" in tool["temporal_roles"]
            for tool in message.get("tools", ())
        )
        for message in messages
    )
    for message in messages:
        message.pop("_supporting", None)
    return OpenCodeProjection(
        selected,
        ("in_range_source_record",) if selected else (),
        all_roles,
        session,
        tuple(messages),
        tuple(children),
        tuple(gaps),
    )
