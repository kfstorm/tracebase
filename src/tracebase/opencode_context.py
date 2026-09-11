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
    session: dict[str, Any]
    messages: tuple[dict[str, Any], ...]

    @property
    def session_id(self) -> str:
        value = self.session.get("value")
        value = value if isinstance(value, dict) else {}
        info = value.get("info")
        identifier = info.get("id") if isinstance(info, dict) else value.get("id")
        if not isinstance(identifier, str):
            raise ArchiveError("OpenCode session payload is invalid")
        return identifier

    @property
    def parent_id(self) -> str | None:
        value = self.session.get("value")
        value = value if isinstance(value, dict) else {}
        info = value.get("info")
        parent = info.get("parentID") if isinstance(info, dict) else None
        if parent is None:
            parent = value.get("parentID")
        return parent if isinstance(parent, str) else None


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


def _session_header(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only session identity and display metadata after parsing."""
    header: dict[str, Any] = {}
    for key in ("id", "directory", "parentID"):
        if key in value:
            header[key] = value[key]
    info = value.get("info")
    if isinstance(info, dict):
        header["info"] = {
            key: info[key]
            for key in ("id", "title", "directory", "parentID")
            if key in info
        }
    return header


def _json(snapshot: PublishedSnapshot) -> dict[str, Any]:
    try:
        value = json.loads(snapshot.evidence["session.json"])
    except KeyError, UnicodeDecodeError, json.JSONDecodeError:
        raise ArchiveError("OpenCode session payload is invalid") from None
    if not isinstance(value, dict):
        raise ArchiveError("OpenCode session payload is invalid")
    return value


def _project_worktree(snapshots: tuple[PublishedSnapshot, ...]) -> str | None:
    for snapshot in reversed(snapshots):
        raw_project = snapshot.evidence.get("project.json")
        if raw_project is None:
            continue
        try:
            project = json.loads(raw_project)
        except UnicodeDecodeError, json.JSONDecodeError:
            continue
        if not isinstance(project, dict):
            continue
        if project.get("id") == "global":
            continue
        worktree = project.get("worktree")
        if isinstance(worktree, str) and worktree and worktree != "/":
            return worktree
    return None


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


def _supporting_message(message: dict[str, Any]) -> bool:
    info = message.get("info")
    return isinstance(info, dict) and (
        info.get("mode") == "compaction"
        or info.get("summary") is True
        or info.get("agent") == "compaction"
    )


def _part_is_supporting(part: dict[str, Any]) -> bool:
    value = part.get("value")
    value = value if isinstance(value, dict) else part
    metadata = value.get("metadata")
    return (
        value.get("type") == "compaction"
        or value.get("synthetic") is True
        or (isinstance(metadata, dict) and metadata.get("compaction_continue") is True)
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
    if finished is None:
        # An unknown end is not an open-ended interval for every later request.
        # Until completion is observed, only the source-native start is known.
        return _point_roles(began, start, end)
    if finished == began:
        return _point_roles(began, start, end)
    in_range = began < end and start < finished
    return tuple(
        role
        for role, present in (
            ("in_range_work", in_range),
            ("earlier_background", finished is not None and finished <= start),
            ("later_progression", began >= end),
        )
        if present
    )


def _part_times(part: dict[str, Any]) -> tuple[datetime, datetime | None] | None:
    time_data = part.get("time")
    if not isinstance(time_data, dict):
        return None
    started = time_data.get("start")
    finished = time_data.get("end")
    if started is None:
        created = time_data.get("created")
        if created is None:
            return None
        if not isinstance(created, (int, float, str)) or isinstance(created, bool):
            raise ArchiveError("OpenCode part payload is invalid")
        created_time = _timestamp(created)
        return created_time, created_time
    if not isinstance(started, (int, float, str)) or isinstance(started, bool):
        raise ArchiveError("OpenCode part payload is invalid")
    start_time = _timestamp(started)
    if finished is None:
        return start_time, None
    if not isinstance(finished, (int, float, str)) or isinstance(finished, bool):
        raise ArchiveError("OpenCode part payload is invalid")
    end_time = _timestamp(finished)
    if end_time < start_time:
        raise ArchiveError("OpenCode part payload is invalid")
    return start_time, end_time


def _part_id(part: dict[str, Any]) -> str:
    identifier = part.get("id", part.get("callID"))
    if isinstance(identifier, str) and identifier:
        return identifier
    return json.dumps(part, sort_keys=True, separators=(",", ":"))


def _refresh_part_roles(part: dict[str, Any], start: datetime, end: datetime) -> None:
    if not part["intervals"]:
        part["temporal_roles"] = ("observed_state",)
        return
    roles: set[str] = set()
    for interval in part["intervals"]:
        roles.update(_interval_roles(interval, start, end))
    part["temporal_roles"] = tuple(sorted(roles))


def _part_is_work(part: dict[str, Any]) -> bool:
    return not _part_is_supporting(part)


def _part_is_task(part: dict[str, Any]) -> bool:
    part_type = part.get("type")
    if not isinstance(part_type, str):
        return False
    value = part.get("value")
    value = value if isinstance(value, dict) else part
    tool = value.get("tool")
    return part_type == "task" or (
        part_type == "tool" and isinstance(tool, str) and tool == "task"
    )


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
    source_id = snapshots[0].manifest["source_id"]
    session: dict[str, Any] = {"value": {}, "representations": []}
    latest_header: dict[str, Any] = {}
    messages_by_id: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        payload = _json(snapshot)
        info = payload.get("info")
        info = info if isinstance(info, dict) else {}
        payload_id = payload.get("id")
        info_id = info.get("id")
        if not any(value == source_id for value in (payload_id, info_id)) or any(
            value is not None and value != source_id for value in (payload_id, info_id)
        ):
            raise ArchiveError("OpenCode session payload identity was invalid")
        latest_header = _session_header(payload)
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
                "temporal_roles": _point_roles(message_time, start, end),
            }
            parts = message.get("parts", ())
            if not isinstance(parts, list):
                raise ArchiveError("OpenCode message payload is invalid")
            if not all(isinstance(part, dict) for part in parts):
                raise ArchiveError("OpenCode message payload is invalid")
            supporting_message = _supporting_message(message)
            record["_created_time"] = message_time
            record["_supporting_message"] = supporting_message
            if supporting_message:
                record["temporal_roles"] = ("observed_state",)
            parts_by_id: dict[str, dict[str, Any]] = {}
            for part in parts:
                if not isinstance(part, dict):
                    raise ArchiveError("OpenCode message payload is invalid")
                part_id = _part_id(part)
                part_interval = (
                    _tool_times(part)
                    if part.get("type") == "tool" or _part_is_task(part)
                    else _part_times(part)
                )
                part_record = parts_by_id.setdefault(
                    part_id,
                    {
                        "id": part_id,
                        "type": part.get("type"),
                        "value": part,
                        "temporal_roles": (
                            _interval_roles(part_interval, start, end)
                            if part_interval is not None
                            else ("observed_state",)
                        ),
                        "intervals": [],
                    },
                )
                if part_interval is not None:
                    part_record["intervals"].append(part_interval)
                    part_record["_start_time"] = part_interval[0]
                    part_record["start"] = part_interval[0].isoformat()
                    if part_interval[1] is not None:
                        part_record["end"] = part_interval[1].isoformat()
                    else:
                        part_record["completion"] = "unknown"
            if parts_by_id:
                record["parts"] = list(parts_by_id.values())
            prior = messages_by_id.get(message_id)
            if prior is None:
                messages_by_id[message_id] = record
            else:
                prior["_supporting_message"] = (
                    prior["_supporting_message"] or record["_supporting_message"]
                )
                if prior["_supporting_message"]:
                    prior["temporal_roles"] = ("observed_state",)
                prior_parts = {part["id"]: part for part in prior.get("parts", ())}
                for part in record.get("parts", ()):
                    prior_part = prior_parts.get(part["id"])
                    if prior_part is None:
                        prior.setdefault("parts", []).append(part)
                    else:
                        prior_part["value"] = part["value"]
                        prior_part["intervals"].extend(part["intervals"])
                        if "_start_time" in part:
                            prior_part["_start_time"] = part["_start_time"]
                            prior_part["start"] = part["start"]
                        if "end" in part:
                            prior_part["end"] = part["end"]
                            prior_part.pop("completion", None)
                        elif "completion" in part:
                            prior_part.pop("end", None)
                            prior_part["completion"] = part["completion"]
                        _refresh_part_roles(prior_part, start, end)
    latest_value = latest_header
    session["value"] = latest_value
    latest_info = latest_value.get("info")
    latest_info = latest_info if isinstance(latest_info, dict) else {}
    directory = latest_info.get("directory", latest_value.get("directory"))
    if not isinstance(directory, str) or not directory:
        for snapshot in reversed(snapshots):
            metadata = snapshot.manifest.get("metadata")
            session_metadata = (
                metadata.get("session") if isinstance(metadata, dict) else None
            )
            candidate = (
                session_metadata.get("directory")
                if isinstance(session_metadata, dict)
                else None
            )
            if isinstance(candidate, str) and candidate:
                directory = candidate
                break
    if isinstance(directory, str) and directory:
        session["working_directory"] = directory
    project_directory = _project_worktree(snapshots)
    if project_directory is None and isinstance(directory, str) and directory:
        project_directory = directory
    if project_directory is not None:
        session["project_directory"] = project_directory
    messages = list(messages_by_id.values())
    messages.sort(key=lambda message: (message["_created_time"], message["id"]))
    for message in messages:
        message.get("parts", []).sort(
            key=lambda tool: (
                "_start_time" not in tool,
                tool.get("_start_time"),
                tool["id"],
            )
        )
        parts = message.get("parts", [])
        if not message["_supporting_message"] and parts:
            retained_parts = [part for part in parts if _part_is_work(part)]
            message["parts"] = retained_parts
            if not retained_parts:
                message["temporal_roles"] = ("observed_state",)
        for part in message.get("parts", ()):
            _refresh_part_roles(part, start, end)
            part.pop("intervals", None)
            part.pop("_start_time", None)
        message.pop("_created_time", None)
    selected = any(
        (
            not message["_supporting_message"]
            and "in_range_work" in message["temporal_roles"]
        )
        or any(
            not message["_supporting_message"]
            and _part_is_work(part)
            and "in_range_work" in part["temporal_roles"]
            for part in message.get("parts", ())
        )
        for message in messages
    )
    if not selected:
        messages = []
    return OpenCodeProjection(
        selected,
        session,
        tuple(messages),
    )
