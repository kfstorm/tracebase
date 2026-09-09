"""Deterministic OpenCode Context Output projection."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .archive import ArchiveError, PublishedSnapshot

_KNOWN_PART_TYPES = {"compaction", "reasoning", "retry", "task", "text", "tool"}


@dataclass(frozen=True, slots=True)
class OpenCodeProjection:
    """The OpenCode-specific, serializable view of one session."""

    selected: bool
    inclusion_reasons: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    session: dict[str, Any]
    messages: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, str], ...]

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
        parent = (
            info.get("parentID") if isinstance(info, dict) else value.get("parentID")
        )
        return parent if isinstance(parent, str) else None

    @property
    def parent_value(self) -> Any:
        value = self.session.get("value")
        value = value if isinstance(value, dict) else {}
        info = value.get("info")
        return info.get("parentID") if isinstance(info, dict) else value.get("parentID")

    @property
    def task_child_references(self) -> tuple[dict[str, str], ...]:
        references: list[dict[str, str]] = []
        for message in self.messages:
            for part in message.get("parts", ()):
                if not _part_is_task(part):
                    continue
                if "in_range_work" not in part.get("temporal_roles", ()):
                    continue
                value = part.get("value")
                value = value if isinstance(value, dict) else {}
                state = value.get("state")
                metadata = state.get("metadata") if isinstance(state, dict) else None
                if not isinstance(metadata, dict):
                    continue
                parent_id = metadata.get("parentSessionId")
                if parent_id != self.session_id:
                    continue
                child_id = metadata.get("sessionId")
                if isinstance(child_id, str) and child_id:
                    references.append({"child_id": child_id})
                else:
                    references.append({"malformed": "true"})
        return tuple(references)


def resolve_opencode_context(
    projections: tuple[OpenCodeProjection, ...],
) -> tuple[OpenCodeProjection, ...]:
    """Expand selected OpenCode sessions through their native session graph."""
    by_session_id = {projection.session_id: projection for projection in projections}
    selected_ids = {
        projection.session_id for projection in projections if projection.selected
    }
    supporting_ids: set[str] = set()
    graph_gaps: dict[str, list[dict[str, str]]] = {}

    for selected_id in selected_ids:
        current_id = selected_id
        current_projection = by_session_id[selected_id]
        parent_value = current_projection.parent_value
        parent_id = current_projection.parent_id
        seen: set[str] = set()
        if parent_value is not None and not isinstance(parent_value, str):
            graph_gaps.setdefault(current_id, []).append(
                {
                    "kind": "malformed-session-parent",
                    "session_id": current_projection.session_id,
                }
            )
        while isinstance(parent_id, str) and parent_id not in seen:
            seen.add(parent_id)
            parent = by_session_id.get(parent_id)
            if parent is None:
                graph_gaps.setdefault(current_id, []).append(
                    {
                        "kind": "missing-session-parent",
                        "session_id": current_projection.session_id,
                        "parent_id": parent_id,
                    }
                )
                break
            supporting_ids.add(parent_id)
            current_id = parent_id
            current_projection = parent
            parent_value = current_projection.parent_value
            if parent_value is not None and not isinstance(parent_value, str):
                graph_gaps.setdefault(current_id, []).append(
                    {
                        "kind": "malformed-session-parent",
                        "session_id": current_projection.session_id,
                    }
                )
                break
            parent_id = current_projection.parent_id
        if isinstance(parent_id, str) and parent_id in seen:
            graph_gaps.setdefault(current_id, []).append(
                {
                    "kind": "cyclic-session-parent",
                    "session_id": current_projection.session_id,
                    "parent_id": parent_id,
                }
            )

        for reference in by_session_id[selected_id].task_child_references:
            if "malformed" in reference:
                graph_gaps.setdefault(selected_id, []).append(
                    {"kind": "malformed-task-child", "session_id": selected_id}
                )
                continue
            child_id = reference["child_id"]
            if child_id in by_session_id:
                supporting_ids.add(child_id)
            else:
                graph_gaps.setdefault(selected_id, []).append(
                    {
                        "kind": "missing-task-child",
                        "session_id": selected_id,
                        "child_id": child_id,
                    }
                )

    resolved: list[OpenCodeProjection] = []
    for original in projections:
        gaps = original.gaps + tuple(graph_gaps.get(original.session_id, ()))
        resolved_projection = original
        if original.session_id in supporting_ids and not original.selected:
            resolved_projection = replace(
                original,
                inclusion_reasons=("supporting-task-context",),
                gaps=gaps,
            )
        elif gaps:
            resolved_projection = replace(original, gaps=gaps)
        resolved.append(resolved_projection)
    return tuple(resolved)


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
        part.get("type") == "compaction"
        or part.get("synthetic") is True
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
    part_type = part.get("type")
    return not isinstance(part_type, str) or part_type != "compaction"


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
    session: dict[str, Any] = {"value": _json(snapshots[-1]), "representations": []}
    messages_by_id: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, str]] = []
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
        session["representations"].append(
            {
                "run_id": snapshot.run["run_id"],
                "observation_window": snapshot.manifest["observation_window"],
                "value": payload,
            }
        )
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
            parts_by_id: dict[str, dict[str, Any]] = {}
            for part in parts:
                if not isinstance(part, dict):
                    raise ArchiveError("OpenCode message payload is invalid")
                part_id = _part_id(part)
                part_type = part.get("type")
                if not isinstance(part_type, str) or part_type not in _KNOWN_PART_TYPES:
                    gaps.append(
                        {
                            "kind": "unknown-part-type",
                            "message_id": message_id,
                            "part_id": part_id,
                            "part_type": str(part_type),
                        }
                    )
                part_interval = (
                    _tool_times(part)
                    if part.get("type") == "tool" or _part_is_task(part)
                    else _part_times(part)
                )
                if part_interval is not None and part_interval[1] is None:
                    gaps.append(
                        {
                            "kind": "unknown-completion",
                            "message_id": message_id,
                            "part_id": part_id,
                        }
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
                        "representations": [],
                        "intervals": [],
                    },
                )
                part_record["representations"].append(
                    {
                        "run_id": snapshot.run["run_id"],
                        "observation_window": snapshot.manifest["observation_window"],
                        "value": part,
                    }
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
                prior["representations"].extend(record["representations"])
                prior["_supporting"] = prior["_supporting"] or record["_supporting"]
                if prior["_supporting"]:
                    prior["temporal_roles"] = ("observed_state",)
                prior_parts = {part["id"]: part for part in prior.get("parts", ())}
                for part in record.get("parts", ()):
                    prior_part = prior_parts.get(part["id"])
                    if prior_part is None:
                        prior.setdefault("parts", []).append(part)
                    else:
                        prior_part["value"] = part["value"]
                        prior_part["representations"].extend(part["representations"])
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
        for part in message.get("parts", ()):
            _refresh_part_roles(part, start, end)
            part.pop("intervals", None)
            part.pop("_start_time", None)
        message.pop("_created_time", None)
    if not any(
        (not record["_supporting"] and "in_range_work" in record["temporal_roles"])
        or any(
            not record["_supporting"]
            and _part_is_work(part)
            and "in_range_work" in part["temporal_roles"]
            for part in record.get("parts", ())
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
                for part in message.get("parts", ())
                if not message["_supporting"] and _part_is_work(part)
                for role in part["temporal_roles"]
            }
        )
    )
    selected = any(
        (not message["_supporting"] and "in_range_work" in message["temporal_roles"])
        or any(
            not message["_supporting"]
            and _part_is_work(part)
            and "in_range_work" in part["temporal_roles"]
            for part in message.get("parts", ())
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
        tuple(gaps),
    )
