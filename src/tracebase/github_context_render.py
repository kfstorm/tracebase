"""Consumer-oriented GitHub Context Output rendering."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import format_timestamp, parse_timestamp
from .github_context import (
    github_actor_login,
    github_inline_comment_canonical_id,
    github_logins_match,
    github_user_work_record_ids,
)
from .github_identity import GitHubIdentity

GITHUB_COMMIT_LIMIT = 250


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


def _commit_dates(
    value: dict[str, Any],
) -> tuple[datetime | None, datetime | None]:
    author = value.get("author")
    committer = value.get("committer")
    author_date = (
        parse_timestamp(author.get("date")) if isinstance(author, dict) else None
    )
    committer_date = (
        parse_timestamp(committer.get("date")) if isinstance(committer, dict) else None
    )
    return author_date, committer_date


def _short_sha(value: Any) -> str | None:
    return value[:7] if isinstance(value, str) and value else None


def _actor_label(value: dict[str, Any], tracked_login: str | None) -> str:
    actor = github_actor_login(value) or "unknown"
    return (
        f"@{actor} (tracked account)"
        if github_logins_match(actor, tracked_login)
        else f"@{actor}"
    )


def _commit_author(value: dict[str, Any], identity: GitHubIdentity | None) -> str:
    author = value.get("author")
    committer = value.get("committer")
    author_name = author.get("name") if isinstance(author, dict) else None
    committer_name = committer.get("name") if isinstance(committer, dict) else None
    if isinstance(author_name, str) and author_name:
        name = author_name
    elif isinstance(committer_name, str) and committer_name:
        name = committer_name
    else:
        name = "unknown"
    author_email = author.get("email") if isinstance(author, dict) else None
    if identity is not None and identity.matches_commit_email(author_email):
        return f"{name} (tracked account)"
    return name


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


def _timeline_event_time(value: dict[str, Any]) -> datetime | None:
    return parse_timestamp(value.get("created_at"))


def _timeline_event_bucket(
    value: dict[str, Any], start: datetime, end: datetime
) -> str | None:
    timestamp = _timeline_event_time(value)
    if timestamp is None:
        return None
    if start <= timestamp < end:
        return "activity"
    return "background" if timestamp < start else None


def _selected_observation_is_after_request_end(
    item: ContextItem, result: ContextExtractionResult
) -> bool:
    window = item.snapshot.manifest.get("observation_window")
    observed_end = (
        parse_timestamp(window.get("to")) if isinstance(window, dict) else None
    )
    return observed_end is not None and observed_end > result.request.end


def _location(value: dict[str, Any]) -> str | None:
    path = value.get("path")
    line = value.get("line", value.get("original_line"))
    if isinstance(path, str) and isinstance(line, int):
        return f"{path}:{line}"
    return path if isinstance(path, str) else None


def _timeline_event_label(
    value: dict[str, Any], tracked_login: str | None
) -> str | None:
    event = value.get("event")
    if event not in {
        "closed",
        "reopened",
        "merged",
        "ready_for_review",
        "converted_to_draft",
        "head_ref_force_pushed",
        "head_ref_restored",
        "base_ref_changed",
        "renamed",
    }:
        return None
    if event == "renamed":
        rename = value.get("rename")
        old_name = rename.get("from") if isinstance(rename, dict) else None
        new_name = rename.get("to") if isinstance(rename, dict) else None
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            return None
        rendered = f"Renamed {old_name} -> {new_name}"
    else:
        rendered = str(event).replace("_", " ").capitalize()
        if event == "head_ref_force_pushed":
            rendered = "Head ref force-pushed"
        if event in {"head_ref_force_pushed", "head_ref_restored"}:
            details: list[str] = []
            for key in ("ref", "commit_id", "before", "after"):
                detail = value.get(key)
                if isinstance(detail, str):
                    if key == "ref":
                        details.append(detail)
                    elif key == "commit_id":
                        details.append(f"commit {_short_sha(detail) or detail}")
                    else:
                        details.append(f"{key} {detail}")
            if details:
                rendered += f" ({'; '.join(details)})"
    actor = github_actor_login(value)
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
    commit_id = _short_sha(value.get("commit_id"))
    commit_suffix = f" · on {commit_id}" if commit_id else ""
    lines = [f"Review by {actor}{suffix}{commit_suffix}{state_suffix}", ""]
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
        if not isinstance(node, dict):
            continue
        canonical_id = github_inline_comment_canonical_id(node)
        if canonical_id is None:
            continue
        value = inline.get(canonical_id, node)
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
    user_work_ids: frozenset[tuple[str, str]],
) -> list[str]:
    location = next(
        (_location(value) for value in [*earlier, *activity] if _location(value)), None
    )
    rendered_time = format_timestamp(timestamp.isoformat(), timezone) or "Unknown time"
    lines = [
        f"### {rendered_time} · Review thread · {location or 'unknown location'}",
        "",
    ]
    for heading, values in (
        ("Earlier context", earlier),
        ("During requested interval", activity),
    ):
        if not values:
            continue
        lines.extend([f"### {heading}", ""])
        for label, selected in (
            (
                "User work",
                [
                    value
                    for value in values
                    if (canonical_id := github_inline_comment_canonical_id(value))
                    is not None
                    and ("inline-comment", canonical_id) in user_work_ids
                ],
            ),
            (
                "Context-only evidence",
                [
                    value
                    for value in values
                    if (canonical_id := github_inline_comment_canonical_id(value))
                    is None
                    or ("inline-comment", canonical_id) not in user_work_ids
                ],
            ),
        ):
            if not selected:
                continue
            lines.extend([f"#### {label}", ""])
            for value in selected:
                _comment(lines, value, timezone, tracked_login)
    return lines


def _thread_entries(
    records: tuple[dict[str, Any], ...],
    start: datetime,
    end: datetime,
    timezone: tzinfo,
    tracked_login: str | None,
    user_work_ids: frozenset[tuple[str, str]],
) -> tuple[list[tuple[datetime, str, list[str], bool]], set[str], set[str], set[str]]:
    inline: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "inline-comment":
            continue
        value = _value(record)
        if isinstance(value, dict):
            inline[str(record.get("native_id"))] = value
    entries: list[tuple[datetime, str, list[str], bool]] = []
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
            canonical_id
            for node in nodes
            if isinstance(node, dict)
            and (canonical_id := github_inline_comment_canonical_id(node)) is not None
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
            thread,
            earlier,
            activity,
            timestamp,
            timezone,
            tracked_login,
            user_work_ids,
        )
        thread_id = str(thread_record.get("native_id"))
        if activity:
            active_thread_ids.add(thread_id)
        else:
            earlier_thread_ids.add(thread_id)
        entries.append(
            (
                timestamp,
                thread_id,
                lines,
                any(
                    (canonical_id := github_inline_comment_canonical_id(value))
                    is not None
                    and ("inline-comment", canonical_id) in user_work_ids
                    for value in [*earlier, *activity]
                ),
            )
        )
    return entries, active_thread_ids, earlier_thread_ids, all_threaded_ids


def _event_entries(  # noqa: PLR0915
    item: ContextItem,
    result: ContextExtractionResult,
    bucket: str,
    user_work_ids: frozenset[tuple[str, str]] | None = None,
) -> list[tuple[datetime, str, list[str], bool]]:
    assert item.github is not None
    projection = item.github
    if user_work_ids is None:
        user_work_ids = github_user_work_record_ids(
            projection, result.request.start, result.request.end
        )
    start, end = result.request.start, result.request.end
    timezone = start.tzinfo
    assert timezone is not None
    records = projection.records
    thread_entries, active_thread_ids, earlier_thread_ids, all_threaded_ids = (
        _thread_entries(
            records,
            start,
            end,
            timezone,
            projection.tracked_login,
            user_work_ids,
        )
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
                            event == "created"
                            and (kind, str(record.get("native_id"))) in user_work_ids,
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
            entries.append(
                (event_time, native_id, lines, (kind, native_id) in user_work_ids)
            )
        elif kind == "timeline":
            value = _value(record)
            if not isinstance(value, dict):
                continue
            label = _timeline_event_label(value, projection.tracked_login)
            if _timeline_event_bucket(value, start, end) != bucket:
                continue
            event_time = _timeline_event_time(value)
            if label is None or event_time is None:
                continue
            rendered_time = format_timestamp(event_time.isoformat(), timezone)
            native_id = str(record.get("native_id"))
            entries.append(
                (
                    event_time,
                    native_id,
                    [f"- {rendered_time} · {label}", ""],
                    (kind, native_id) in user_work_ids,
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
            native_id = str(record.get("native_id"))
            entries.append(
                (
                    event_time,
                    native_id,
                    review_lines,
                    (kind, native_id) in user_work_ids,
                )
            )
    return sorted(entries, key=lambda entry: (entry[0], entry[1]))


def _commit_limit_warning(records: tuple[dict[str, Any], ...]) -> str | None:
    observed_commits = 0
    for record in records:
        if record.get("kind") != "timeline":
            continue
        value = _value(record)
        if isinstance(value, dict) and value.get("event") == "committed":
            observed_commits += 1
    if observed_commits >= GITHUB_COMMIT_LIMIT:
        return (
            "Observed GitHub Timeline commit events reached 250 entries; this "
            "section may be incomplete."
        )
    for record in records:
        if record.get("kind") != "pull-request-payload":
            continue
        value = _value(record)
        commits = value.get("commits") if isinstance(value, dict) else None
        if (
            isinstance(commits, int)
            and not isinstance(commits, bool)
            and commits > GITHUB_COMMIT_LIMIT
        ):
            return (
                "GitHub may truncate PR commit history at 250 entries; this section "
                "may be incomplete."
            )
    return None


def _commit_section(
    records: tuple[dict[str, Any], ...],
    start: datetime,
    end: datetime,
    bucket: str,
    timezone: tzinfo,
    warn_without_commits: bool,
    identity: GitHubIdentity | None,
    user_work_ids: frozenset[tuple[str, str]],
    user_only: bool,
) -> list[str]:
    commits: list[tuple[datetime, str, str, str, datetime | None, int]] = []
    fallback_order = 0
    for record in records:
        if record.get("kind") != "timeline":
            continue
        value = _value(record)
        if not isinstance(value, dict) or value.get("event") != "committed":
            continue
        native_id = str(record.get("native_id"))
        is_user_work = ("timeline", native_id) in user_work_ids
        if is_user_work != user_only:
            continue
        author_date, committer_date = _commit_dates(value)
        timestamp = committer_date or author_date
        sha = _short_sha(value.get("sha"))
        if timestamp is None or sha is None:
            continue
        if bucket == "activity" and not start <= timestamp < end:
            continue
        if bucket == "background" and not timestamp < start:
            continue
        message = value.get("message")
        source_order = record.get("source_order")
        order = source_order if isinstance(source_order, int) else fallback_order
        fallback_order += 1
        commits.append(
            (
                timestamp,
                sha,
                message if isinstance(message, str) else "",
                _commit_author(value, identity),
                author_date
                if author_date is not None
                and committer_date is not None
                and author_date != committer_date
                else None,
                order,
            )
        )
    warning = _commit_limit_warning(records)
    if not commits and (warning is None or not warn_without_commits):
        return []

    lines = ["## Commits", ""]
    if commits:
        lines.extend(
            [
                "Commit placement uses Git committer time, falling back to author "
                "time when absent. Differing author times are shown separately. "
                "Neither timestamp is GitHub push time.",
                "",
            ]
        )
    for timestamp, sha, message, author, author_date, _order in sorted(
        commits, key=lambda entry: entry[-1]
    ):
        rendered_time = format_timestamp(timestamp.isoformat(), timezone)
        if rendered_time is None:
            continue
        lines.append(f"- {rendered_time} · `{sha}` · {author}")
        if author_date is not None:
            rendered_author_time = format_timestamp(author_date.isoformat(), timezone)
            if rendered_author_time is not None:
                lines.append(f"  Authored: {rendered_author_time}")
        if message:
            lines.extend(f"  {line}" if line else "" for line in message.split("\n"))
        lines.append("")
    if warning is not None:
        lines.extend([warning, ""])
    return lines


def _activity(
    item: ContextItem,
    result: ContextExtractionResult,
    bucket: str,
    user_work_ids: frozenset[tuple[str, str]],
) -> list[str]:
    assert item.github is not None
    entries = _event_entries(item, result, bucket, user_work_ids)
    lines = [
        f"# {'Activity' if bucket == 'activity' else 'Background'}",
        "",
        f"Attribution mode: `{item.attribution_mode.value}`",
        "",
    ]
    timezone = result.request.start.tzinfo
    assert timezone is not None
    for label, user_only in (("User work", True), ("Context-only evidence", False)):
        selected_entries = [entry for entry in entries if entry[3] == user_only]
        commit_lines = _commit_section(
            item.github.records,
            result.request.start,
            result.request.end,
            bucket,
            timezone,
            bucket == "activity" and not user_only,
            item.github.tracked_identity,
            user_work_ids,
            user_only,
        )
        if not selected_entries and not commit_lines:
            continue
        lines.extend([f"## {label}", ""])
        for _, _, entry_lines, _ in selected_entries:
            lines.extend(entry_lines)
        lines.extend(commit_lines)
    if lines == [
        f"# {'Activity' if bucket == 'activity' else 'Background'}",
        "",
        f"Attribution mode: `{item.attribution_mode.value}`",
        "",
    ]:
        return []
    return lines


def _overview(
    item: ContextItem,
    result: ContextExtractionResult,
    user_work_ids: frozenset[tuple[str, str]],
) -> list[str]:
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
    lines.extend(
        [
            "## Tracked account",
            "",
            f"- GitHub: @{projection.tracked_login or 'unknown'}",
            f"- Attribution mode: `{item.attribution_mode.value}`",
            "- User-work projection: only tracked-account actions and authorship "
            "marked in Activity are eligible for the personal Summary.",
            "- Collaborator records, implementation, findings, and decisions are "
            "context-only evidence and must not become Summary content.",
            "- Git commit identities are marked `(tracked account)` only when they "
            "match a locally synced identity profile.",
            "- Activity and Background may include collaborators' work on tracked "
            "Items; only explicitly marked actors are the tracked account.",
            "",
        ]
    )
    if not user_work_ids:
        lines.extend(
            [
                "No tracked-account user work was identified in the requested "
                "interval; this Item is context-only for personal Summary purposes.",
                "",
            ]
        )
    lines.extend(["## Context-only item state", ""])
    author = github_actor_login(value)
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
    if _selected_observation_is_after_request_end(item, result):
        lines.extend(
            [
                "",
                "Mutable fields may include later-observed changes and are not "
                "guaranteed to equal the exact state at the request end.",
            ]
        )
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
    user_work_ids = github_user_work_record_ids(
        item.github, result.request.start, result.request.end
    )
    files: dict[str, list[str] | bytes] = {
        "overview.md": _overview(item, result, user_work_ids)
    }
    activity = _activity(item, result, "activity", user_work_ids)
    background = _activity(item, result, "background", user_work_ids)
    diff = _diff_content(item, result)
    if activity:
        files["activity.md"] = activity
    if background:
        files["background.md"] = background
    if diff is not None:
        files["diff.patch"] = diff
    return files
