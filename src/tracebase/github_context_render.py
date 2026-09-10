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


def _value(record: dict[str, Any], end: datetime) -> dict[str, Any] | str | None:
    candidates: list[dict[str, Any]] = []
    for representation in _representations(record):
        value = representation.get("value")
        if isinstance(value, str):
            candidates.append(representation)
            continue
        if not isinstance(value, dict):
            continue
        times = _times(value)
        if not times or any(timestamp < end for timestamp in times):
            candidates.append(representation)
    if not candidates:
        return None
    value = candidates[-1].get("value")
    return value if isinstance(value, (dict, str)) else None


def _times(value: dict[str, Any]) -> tuple[datetime, ...]:
    keys = ("created_at", "updated_at", "submitted_at", "merged_at", "closed_at")
    parsed = [
        parse_timestamp(value[key]) for key in keys if isinstance(value.get(key), str)
    ]
    return tuple(value for value in parsed if value is not None)


def _bucket(record: dict[str, Any], start: datetime, end: datetime) -> str | None:
    timestamps = [
        timestamp
        for representation in _representations(record)
        if isinstance(representation.get("value"), dict)
        for timestamp in _times(representation["value"])
    ]
    if any(start <= timestamp < end for timestamp in timestamps):
        return "activity"
    if any(timestamp < start for timestamp in timestamps):
        return "background"
    return None


def _actor(value: dict[str, Any]) -> str | None:
    for key in ("user", "actor", "author"):
        actor = value.get(key)
        if isinstance(actor, dict):
            login = actor.get("login")
            if isinstance(login, str):
                return login
    return None


def _time(value: dict[str, Any], timezone: tzinfo) -> str | None:
    for key in ("created_at", "submitted_at", "updated_at"):
        result = format_timestamp(value.get(key), timezone)
        if result is not None:
            return result
    return None


def _edited_time(value: dict[str, Any], timezone: tzinfo) -> str | None:
    created = parse_timestamp(value.get("created_at"))
    updated = parse_timestamp(value.get("updated_at"))
    if created is None or updated is None or created == updated:
        return None
    return format_timestamp(value.get("updated_at"), timezone)


def _comment(lines: list[str], value: dict[str, Any], timezone: tzinfo) -> None:
    actor = _actor(value) or "unknown"
    created = _time(value, timezone)
    edited = _edited_time(value, timezone)
    timestamp = f" · {created}" if created else ""
    if edited:
        timestamp += f" · edited {edited}"
    lines.extend([f"**@{actor}{timestamp}**", ""])
    body = value.get("body")
    if isinstance(body, str) and body:
        lines.extend([body, ""])


def _record_by_kind(
    records: tuple[dict[str, Any], ...], kind: str
) -> list[dict[str, Any]]:
    return [record for record in records if record.get("kind") == kind]


def _overview(item: ContextItem, result: ContextExtractionResult) -> list[str]:
    assert item.github is not None
    projection = item.github
    end = result.request.end
    object_record = next(
        (
            record
            for record in projection.records
            if record.get("kind") in {"issue", "pull-request"}
        ),
        None,
    )
    value = _value(object_record, end) if object_record else None
    value = value if isinstance(value, dict) else {}
    kind = (
        "PR"
        if any(record.get("kind") == "pull-request" for record in projection.records)
        else "Issue"
    )
    title = projection.title or value.get("title") or "Untitled"
    lines = [f"# {projection.repository} {kind} #{projection.number} — {title}", ""]
    author = _actor(value)
    if author:
        lines.append(f"- Author: @{author}")
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
    if any(record.get("kind") == "aggregate-diff" for record in projection.records):
        lines.extend(
            ["", "## Code changes", "", "Full diff: [diff.patch](diff.patch)", ""]
        )
    return lines


def _activity(
    item: ContextItem, result: ContextExtractionResult, bucket: str
) -> list[str]:
    assert item.github is not None
    projection = item.github
    timezone = result.request.start.tzinfo
    assert timezone is not None
    lines = ["# Activity", ""]
    records = projection.records
    for record in records:
        if _bucket(record, result.request.start, result.request.end) != bucket:
            continue
        kind = record.get("kind")
        value = _value(record, result.request.end)
        if not isinstance(value, dict):
            continue
        if kind == "ordinary-comment":
            _comment(lines, value, timezone)
        elif kind == "timeline" and value.get("event") in {
            "closed",
            "reopened",
            "merged",
            "ready_for_review",
            "converted_to_draft",
        }:
            actor = _actor(value)
            event = str(value.get("event"))
            timestamp = _time(value, timezone)
            suffix = f" · {timestamp}" if timestamp else ""
            by = f" by @{actor}" if actor else ""
            lines.extend([f"- {event.replace('_', ' ').capitalize()}{by}{suffix}", ""])
    if lines == ["# Activity", ""]:
        return []
    return lines


def _location(value: dict[str, Any]) -> str | None:
    path = value.get("path")
    line = value.get("line", value.get("original_line"))
    if isinstance(path, str) and isinstance(line, int):
        return f"{path}:{line}"
    return path if isinstance(path, str) else None


def _review_submission(
    lines: list[str], value: dict[str, Any], timezone: tzinfo
) -> None:
    actor = _actor(value) or "unknown"
    timestamp = _time(value, timezone)
    suffix = f" · {timestamp}" if timestamp else ""
    state = value.get("state")
    state_suffix = f" ({state})" if isinstance(state, str) else ""
    lines.extend([f"### Review by @{actor}{suffix}{state_suffix}", ""])
    body = value.get("body")
    if isinstance(body, str) and body:
        lines.extend([body, ""])


def _thread_lines(
    lines: list[str],
    thread: dict[str, Any],
    inline: dict[str, dict[str, Any]],
    timezone: tzinfo,
) -> None:
    comments = thread.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    if not isinstance(nodes, list):
        return
    values: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        value = inline.get(node_id) if isinstance(node_id, str) else None
        if value is not None:
            values.append(value)
    if not values:
        return
    location = next((_location(value) for value in values if _location(value)), None)
    lines.extend([f"### `{location or 'Review thread'}`", ""])
    for value in values:
        _comment(lines, value, timezone)


def _reviews(item: ContextItem, result: ContextExtractionResult) -> list[str]:
    assert item.github is not None
    projection = item.github
    timezone = result.request.start.tzinfo
    assert timezone is not None
    lines = ["# Reviews", ""]
    records = projection.records
    inline: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "inline-comment":
            continue
        value = _value(record, result.request.end)
        if isinstance(value, dict):
            inline[str(record.get("native_id"))] = value
    threaded_ids: set[str] = set()
    for thread_record in _record_by_kind(records, "review-thread"):
        value = _value(thread_record, result.request.end)
        if not isinstance(value, dict):
            continue
        comments = value.get("comments")
        nodes = comments.get("nodes") if isinstance(comments, dict) else None
        if isinstance(nodes, list):
            threaded_ids.update(
                str(node["id"])
                for node in nodes
                if isinstance(node, dict) and isinstance(node.get("id"), str)
            )
        _thread_lines(lines, value, inline, timezone)
    for record in records:
        kind = record.get("kind")
        if kind not in {"review", "inline-comment"}:
            continue
        if kind == "inline-comment" and str(record.get("native_id")) in threaded_ids:
            continue
        if _bucket(record, result.request.start, result.request.end) is None:
            continue
        value = _value(record, result.request.end)
        if not isinstance(value, dict):
            continue
        if kind == "review":
            _review_submission(lines, value, timezone)
        else:
            location = _location(value)
            if location:
                lines.extend([f"### Unthreaded review comment at `{location}`", ""])
            _comment(lines, value, timezone)
    return [] if lines == ["# Reviews", ""] else lines


def _diff_content(item: ContextItem, result: ContextExtractionResult) -> list[str]:
    assert item.github is not None
    record = next(
        (
            record
            for record in item.github.records
            if record.get("kind") == "aggregate-diff"
        ),
        None,
    )
    value = _value(record, result.request.end) if record else None
    return value.splitlines() if isinstance(value, str) else []


def render_github(
    item: ContextItem, result: ContextExtractionResult
) -> dict[str, list[str]]:
    assert item.github is not None
    files: dict[str, list[str]] = {"overview.md": _overview(item, result)}
    activity = _activity(item, result, "activity")
    background = _activity(item, result, "background")
    reviews = _reviews(item, result)
    diff = any(record.get("kind") == "aggregate-diff" for record in item.github.records)
    if activity:
        files["activity.md"] = activity
    if background:
        files["background.md"] = background
    if reviews:
        files["reviews.md"] = reviews
    if diff and _diff_content(item, result):
        files["diff.patch"] = _diff_content(item, result)
    return files
