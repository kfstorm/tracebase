"""Consumer-oriented OpenCode Context Output rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import format_timestamp

_BACKGROUND_USER_TURN_LIMIT = 3
_MINIMUM_RENDERED_LINES = 2


def _session_context(item: ContextItem) -> dict[str, str]:
    projection = item.opencode
    if projection is None:
        return {}
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    context: dict[str, str] = {}
    for source in (info, value):
        candidate = source.get("directory")
        if isinstance(candidate, str) and candidate:
            context["working_directory"] = candidate
            break
    for snapshot in reversed(item.snapshots):
        metadata = snapshot.manifest.get("metadata")
        session = metadata.get("session") if isinstance(metadata, dict) else None
        if isinstance(session, dict):
            candidate = session.get("directory")
            if (
                isinstance(candidate, str)
                and candidate
                and "working_directory" not in context
            ):
                context["working_directory"] = candidate
        project = None
        raw_project = snapshot.evidence.get("project.json")
        if raw_project is not None:
            try:
                project = json.loads(raw_project)
            except UnicodeDecodeError, json.JSONDecodeError:
                project = None
        if isinstance(project, dict):
            candidate = project.get("worktree")
            if (
                isinstance(candidate, str)
                and candidate
                and project.get("id") != "global"
                and candidate != "/"
                and "project_directory" not in context
            ):
                context["project_directory"] = candidate
    session_project = projection.session.get("project_directory")
    if isinstance(session_project, str) and session_project:
        context["project_directory"] = session_project
    if "project_directory" not in context and "working_directory" in context:
        context["project_directory"] = context["working_directory"]
    return context


def _part_value(part: dict[str, Any]) -> dict[str, Any]:
    value = part.get("value")
    return value if isinstance(value, dict) else part


def _part_bucket(part: dict[str, Any], message: dict[str, Any]) -> str | None:
    roles = part.get("temporal_roles", ())
    bucket: str | None = None
    if "later_progression" in roles:
        return bucket
    if "in_range_work" in roles:
        bucket = "activity"
    elif "earlier_background" in roles:
        bucket = "background"
    elif part.get("type") == "text":
        message_roles = message.get("temporal_roles", ())
        if "in_range_work" in message_roles:
            bucket = "activity"
        elif "earlier_background" in message_roles:
            bucket = "background"
    return bucket


def _header(role: str, timestamp: str | None) -> str:
    role = role.capitalize() if role else "Message"
    return f"**{role}{f' · {timestamp}' if timestamp else ''}**"


def _render_messages(
    item: ContextItem, result: ContextExtractionResult, bucket: str
) -> list[str]:
    assert item.opencode is not None
    projection = item.opencode
    timezone = result.request.start.tzinfo
    assert timezone is not None
    lines = [f"# {'Activity' if bucket == 'activity' else 'Background'}", ""]
    messages: list[tuple[dict[str, Any], str, list[str]]] = []
    for message in projection.messages:
        role = message.get("role")
        role = role.lower() if isinstance(role, str) else ""
        if role not in {"user", "assistant"}:
            continue
        texts: list[str] = []
        for part in message.get("parts", ()):
            if (
                not isinstance(part, dict)
                or _part_bucket(part, message) != bucket
                or part.get("type") != "text"
            ):
                continue
            value = _part_value(part)
            text = value.get("text", part.get("text"))
            if isinstance(text, str) and text:
                texts.append(text)
        if texts:
            messages.append((message, role, texts))

    if bucket == "background":
        user_positions = [
            index for index, (_, role, _) in enumerate(messages) if role == "user"
        ]
        if user_positions:
            first_retained = user_positions[
                max(0, len(user_positions) - _BACKGROUND_USER_TURN_LIMIT)
            ]
            messages = messages[first_retained:]

    for message, role, texts in messages:
        timestamp = format_timestamp(message.get("created"), timezone)
        lines.extend([_header(str(role), timestamp), ""])
        for text in texts:
            lines.extend([text, ""])
    return lines if len(lines) > _MINIMUM_RENDERED_LINES else []


def render_opencode(
    item: ContextItem, result: ContextExtractionResult, output: Path
) -> list[str]:
    assert item.opencode is not None
    projection = item.opencode
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    title = info.get("title")
    title = title if isinstance(title, str) and title else "Untitled session"
    context = _session_context(item)
    directory = context.get("working_directory", ".")
    project_directory = context.get("project_directory", directory)
    overview = [f"# {title}", "", f"Project directory: `{project_directory}`"]
    if directory != project_directory:
        overview.append(f"Working directory: `{directory}`")
    overview.append("")
    files: dict[str, list[str]] = {"overview.md": overview}
    activity = _render_messages(item, result, "activity")
    background = _render_messages(item, result, "background")
    if activity:
        files["activity.md"] = activity
    if background:
        files["background.md"] = background
    for name, lines in files.items():
        write_opencode_markdown(output / name, lines)
    return list(files)


def write_opencode_markdown(path: Path, lines: list[str]) -> None:
    """Write OpenCode text without changing Markdown trailing spaces."""
    content_lines = lines[:-1] if lines and lines[-1] == "" else lines
    path.write_text("\n".join(content_lines) + "\n", encoding="utf-8")
