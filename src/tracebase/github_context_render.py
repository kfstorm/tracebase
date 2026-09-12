"""Consumer-oriented GitHub Context Output rendering."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import format_timestamp, parse_timestamp


def _representations(record: dict[str, Any]) -> list[dict[str, Any]]:
    values = record.get("representations")
    return (
        [value for value in values if isinstance(value, dict)]
        if isinstance(values, list)
        else []
    )


def _representation_value(
    record: dict[str, Any],
) -> dict[str, Any] | str | bytes | None:
    representations = _representations(record)
    if not representations:
        return None
    value = representations[-1].get("value")
    selected = value if isinstance(value, (dict, str, bytes)) else None
    return selected


def _value(record: dict[str, Any] | None) -> dict[str, Any] | str | None:
    if record is None:
        return None
    value = _representation_value(record)
    return value if isinstance(value, (dict, str)) else None


def _times(value: dict[str, Any]) -> tuple[datetime, ...]:
    keys = ("created_at", "updated_at", "submitted_at", "merged_at", "closed_at")
    return tuple(
        parsed
        for key in keys
        if (parsed := parse_timestamp(value.get(key))) is not None
    )


def _actor(value: dict[str, Any]) -> str | None:
    for key in ("user", "actor", "author"):
        actor = value.get(key)
        login = actor.get("login") if isinstance(actor, dict) else None
        if isinstance(login, str):
            return login
    return None


def _actor_label(value: dict[str, Any], tracked_login: str | None) -> str:
    actor = _actor(value) or "unknown"
    return f"@{actor} (tracked account)" if actor == tracked_login else f"@{actor}"


def _time(value: dict[str, Any], timezone: tzinfo) -> str | None:
    for key in ("created_at", "submitted_at", "updated_at"):
        result = format_timestamp(value.get(key), timezone)
        if result is not None:
            return result
    return None


def _edited_time(value: dict[str, Any], timezone: tzinfo) -> str | None:
    created = format_timestamp(value.get("created_at"), timezone)
    updated = format_timestamp(value.get("updated_at"), timezone)
    if created is None or updated is None or created == updated:
        return None
    return updated


def _comment(
    lines: list[str], value: dict[str, Any], timezone: tzinfo, tracked_login: str | None
) -> None:
    actor = _actor_label(value, tracked_login)
    created = _time(value, timezone)
    edited = _edited_time(value, timezone)
    timestamp = f" · {created}" if created else ""
    if edited:
        timestamp += f" · edited {edited}"
    lines.extend([f"**{actor}{timestamp}**", ""])
    body = value.get("body")
    if isinstance(body, str) and body:
        lines.extend([body, ""])


def _record_by_kind(
    records: tuple[dict[str, Any], ...], kind: str
) -> list[dict[str, Any]]:
    return [record for record in records if record.get("kind") == kind]


def _in_range_time(
    value: dict[str, Any], start: datetime, end: datetime
) -> datetime | None:
    times = [timestamp for timestamp in _times(value) if start <= timestamp < end]
    return min(times) if times else None


def _event_bucket(value: dict[str, Any], start: datetime, end: datetime) -> str | None:
    times = _times(value)
    if any(start <= timestamp < end for timestamp in times):
        return "activity"
    if any(timestamp < start for timestamp in times):
        return "background"
    return None


def _location(value: dict[str, Any]) -> str | None:
    path = value.get("path")
    line = value.get("line", value.get("original_line"))
    if isinstance(path, str) and isinstance(line, int):
        return f"{path}:{line}"
    return path if isinstance(path, str) else None


def _lifecycle(value: dict[str, Any], tracked_login: str | None) -> str | None:
    event = value.get("event")
    if event not in {
        "closed",
        "reopened",
        "merged",
        "ready_for_review",
        "converted_to_draft",
    }:
        return None
    actor = _actor(value)
    rendered = str(event).replace("_", " ").capitalize()
    return f"{rendered}{f' by {_actor_label(value, tracked_login)}' if actor else ''}"


def _object_events(
    record: dict[str, Any], start: datetime, end: datetime
) -> list[tuple[datetime, str]]:
    seen: set[tuple[str, datetime]] = set()
    for representation in _representations(record):
        value = representation.get("value")
        if not isinstance(value, dict):
            continue
        created = parse_timestamp(value.get("created_at"))
        updated = parse_timestamp(value.get("updated_at"))
        if created is not None and start <= created < end:
            seen.add(("created", created))
        if (
            updated is not None
            and start <= updated < end
            and (created is None or updated != created)
        ):
            seen.add(("updated", updated))
    return [
        (timestamp, event)
        for event, timestamp in sorted(seen, key=lambda value: (value[1], value[0]))
    ]


def _review_lines(
    value: dict[str, Any], timezone: tzinfo, tracked_login: str | None
) -> list[str] | None:
    state = value.get("state")
    body = value.get("body")
    if isinstance(state, str) and state.upper() == "COMMENTED" and not body:
        return None
    actor = _actor_label(value, tracked_login)
    timestamp = _time(value, timezone)
    state_suffix = f" ({state})" if isinstance(state, str) else ""
    suffix = f" · {timestamp}" if timestamp else ""
    lines = [f"Review by {actor}{suffix}{state_suffix}", ""]
    if isinstance(body, str) and body:
        lines.extend([body, ""])
    return lines


def _thread_comments(
    thread: dict[str, Any],
    inline: dict[str, dict[str, Any]],
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    comments = thread.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    if not isinstance(nodes, list):
        return [], [], []
    earlier: list[dict[str, Any]] = []
    activity: list[dict[str, Any]] = []
    future: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        value = inline.get(node["id"], node)
        times = _times(value)
        if any(start <= timestamp < end for timestamp in times):
            activity.append(value)
        elif any(timestamp < start for timestamp in times):
            earlier.append(value)
        else:
            future.append(value)
    return earlier, activity, future


def _thread_lines(
    thread: dict[str, Any],
    earlier: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    timestamp: datetime,
    timezone: tzinfo,
    tracked_login: str | None,
) -> list[str]:
    location = next(
        (_location(value) for value in [*earlier, *activity] if _location(value)), None
    )
    rendered_time = format_timestamp(timestamp.isoformat(), timezone) or "Unknown time"
    lines = [
        f"### {rendered_time} · Review thread · {location or 'unknown location'}",
        "",
    ]
    if earlier:
        lines.extend(["### Earlier context", ""])
        for value in earlier:
            _comment(lines, value, timezone, tracked_login)
    if activity:
        lines.extend(["### During requested interval", ""])
        for value in activity:
            _comment(lines, value, timezone, tracked_login)
    return lines


def _thread_entries(
    records: tuple[dict[str, Any], ...],
    start: datetime,
    end: datetime,
    timezone: tzinfo,
    tracked_login: str | None,
) -> tuple[list[tuple[datetime, str, list[str]]], set[str], set[str], set[str]]:
    inline: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "inline-comment":
            continue
        value = _value(record)
        if isinstance(value, dict):
            inline[str(record.get("native_id"))] = value
    entries: list[tuple[datetime, str, list[str]]] = []
    active_thread_ids: set[str] = set()
    earlier_thread_ids: set[str] = set()
    all_threaded_ids: set[str] = set()
    for thread_record in _record_by_kind(records, "review-thread"):
        thread = _value(thread_record)
        if not isinstance(thread, dict):
            continue
        earlier, activity, _future = _thread_comments(thread, inline, start, end)
        comments = thread.get("comments")
        nodes = comments.get("nodes") if isinstance(comments, dict) else []
        if not isinstance(nodes, list):
            nodes = []
        all_threaded_ids.update(
            str(node["id"])
            for node in nodes
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        )
        if not earlier and not activity:
            continue
        if activity:
            activity_times = [
                event_time
                for value in activity
                if (event_time := _in_range_time(value, start, end)) is not None
            ]
            timestamp = min(activity_times)
        else:
            timestamp = min(
                timestamp
                for value in earlier
                for timestamp in _times(value)
                if timestamp < start
            )
        lines = _thread_lines(
            thread, earlier, activity, timestamp, timezone, tracked_login
        )
        thread_id = str(thread_record.get("native_id"))
        if activity:
            active_thread_ids.add(thread_id)
        else:
            earlier_thread_ids.add(thread_id)
        entries.append((timestamp, thread_id, lines))
    return entries, active_thread_ids, earlier_thread_ids, all_threaded_ids


def _event_entries(  # noqa: PLR0915
    item: ContextItem,
    result: ContextExtractionResult,
    bucket: str,
) -> list[tuple[datetime, str, list[str]]]:
    assert item.github is not None
    projection = item.github
    start, end = result.request.start, result.request.end
    timezone = start.tzinfo
    assert timezone is not None
    records = projection.records
    thread_entries, active_thread_ids, earlier_thread_ids, all_threaded_ids = (
        _thread_entries(records, start, end, timezone, projection.tracked_login)
    )
    entries = [
        entry
        for entry in thread_entries
        if (bucket == "activity" and entry[1] in active_thread_ids)
        or (bucket == "background" and entry[1] in earlier_thread_ids)
    ]
    object_kinds = {"issue", "pull-request"}
    for record in records:
        kind = record.get("kind")
        if kind in object_kinds and bucket == "activity":
            object_name = "PR" if kind == "pull-request" else "Issue"
            for timestamp, event in _object_events(record, start, end):
                rendered_time = format_timestamp(timestamp.isoformat(), timezone)
                if rendered_time is not None:
                    entries.append(
                        (
                            timestamp,
                            f"{kind}:{event}",
                            [f"{rendered_time} · {object_name} {event}", ""],
                        )
                    )
            continue
        if kind in {"ordinary-comment", "inline-comment"}:
            native_id = str(record.get("native_id"))
            if kind == "inline-comment" and native_id in all_threaded_ids:
                continue
            value = _value(record)
            if not isinstance(value, dict):
                continue
            if _event_bucket(value, start, end) != bucket:
                continue
            event_time = _in_range_time(value, start, end)
            if event_time is None:
                event_time = min(
                    (time for time in _times(value) if time < start), default=None
                )
            if event_time is None:
                continue
            lines: list[str] = []
            _comment(lines, value, timezone, projection.tracked_login)
            entries.append((event_time, native_id, lines))
        elif kind == "timeline":
            value = _value(record)
            if not isinstance(value, dict):
                continue
            label = _lifecycle(value, projection.tracked_login)
            if _event_bucket(value, start, end) != bucket:
                continue
            event_time = _in_range_time(value, start, end)
            if label is None:
                continue
            if event_time is None:
                event_time = min(
                    (time for time in _times(value) if time < start), default=None
                )
                if event_time is None:
                    continue
            rendered_time = format_timestamp(event_time.isoformat(), timezone)
            entries.append(
                (
                    event_time,
                    str(record.get("native_id")),
                    [f"- {rendered_time} · {label}", ""],
                )
            )
        elif kind == "review":
            value = _value(record)
            if not isinstance(value, dict):
                continue
            review_lines = _review_lines(value, timezone, projection.tracked_login)
            if _event_bucket(value, start, end) != bucket:
                continue
            event_time = _in_range_time(value, start, end)
            if review_lines is None:
                continue
            if event_time is None:
                event_time = min(
                    (time for time in _times(value) if time < start), default=None
                )
                if event_time is None:
                    continue
            entries.append((event_time, str(record.get("native_id")), review_lines))
    return sorted(entries, key=lambda entry: (entry[0], entry[1]))


def _activity(
    item: ContextItem, result: ContextExtractionResult, bucket: str
) -> list[str]:
    entries = _event_entries(item, result, bucket)
    if not entries:
        return []
    lines = [f"# {'Activity' if bucket == 'activity' else 'Background'}", ""]
    for _, _, entry_lines in entries:
        lines.extend(entry_lines)
    return lines


def _overview(item: ContextItem, result: ContextExtractionResult) -> list[str]:
    assert item.github is not None
    projection = item.github
    object_record = next(
        (
            record
            for record in projection.records
            if record.get("kind") in {"issue", "pull-request"}
        ),
        None,
    )
    value = _representation_value(object_record) if object_record is not None else None
    value = value if isinstance(value, dict) else {}
    kind = (
        "PR"
        if any(record.get("kind") == "pull-request" for record in projection.records)
        else "Issue"
    )
    title = value.get("title") or projection.title or "Untitled"
    lines = [f"# {projection.repository} {kind} #{projection.number} — {title}", ""]
    author = _actor(value)
    if author:
        lines.append(f"- Author: {_actor_label(value, projection.tracked_login)}")
    lines.append(f"- Type: {'Pull request' if kind == 'PR' else 'Issue'}")
    for label, key in (("State", "state"), ("Merged", "merged"), ("Draft", "draft")):
        if isinstance(value.get(key), (str, bool)):
            rendered = (
                str(value[key]).lower() if isinstance(value[key], bool) else value[key]
            )
            lines.append(f"- {label}: {rendered}")
    body = value.get("body")
    if isinstance(body, str) and body:
        lines.extend(["", "## Description", "", body, ""])
    diff_record = next(
        (
            record
            for record in projection.records
            if record.get("kind") == "aggregate-diff"
        ),
        None,
    )
    if diff_record is not None:
        lines.extend(
            ["", "## Code changes", "", "Full diff: [diff.patch](diff.patch)", ""]
        )
    return lines


def _diff_content(item: ContextItem, result: ContextExtractionResult) -> bytes | None:
    assert item.github is not None
    record = next(
        (
            record
            for record in item.github.records
            if record.get("kind") == "aggregate-diff"
        ),
        None,
    )
    if record is None:
        return None
    value = _representation_value(record)
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8") if isinstance(value, str) else None


def render_github(
    item: ContextItem, result: ContextExtractionResult
) -> dict[str, list[str] | bytes]:
    assert item.github is not None
    files: dict[str, list[str] | bytes] = {"overview.md": _overview(item, result)}
    activity = _activity(item, result, "activity")
    background = _activity(item, result, "background")
    diff = _diff_content(item, result)
    if activity:
        files["activity.md"] = activity
    if background:
        files["background.md"] = background
    if diff is not None:
        files["diff.patch"] = diff
    return files
