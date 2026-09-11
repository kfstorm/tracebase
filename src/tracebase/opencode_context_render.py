"""Consumer-oriented OpenCode Context Output rendering."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import format_timestamp

_MESSAGE_HEADER = re.compile(
    r"^\*\*(User|Assistant)(?: · \d{4}-\d{2}-\d{2} \d{2}:\d{2})?\*\*$"
)
_BACKGROUND_USER_TURN_LIMIT = 3
_MINIMUM_RENDERED_LINES = 2
_MINIMUM_DUPLICATE_OCCURRENCES = 2
_REPEATED_TEXT_DIGEST_LENGTH = 16
_REPEATED_TEXT_MINIMUM_BYTES = 200


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


def _message_bucket(message: dict[str, Any]) -> str | None:
    roles = message.get("temporal_roles", ())
    parts = message.get("parts", ())
    if any(
        "in_range_work" in part.get("temporal_roles", ())
        for part in parts
        if isinstance(part, dict)
    ):
        return "activity"
    if any(
        "earlier_background" in part.get("temporal_roles", ())
        for part in parts
        if isinstance(part, dict)
    ):
        return "background"
    if "in_range_work" in roles:
        return "activity"
    if "earlier_background" in roles:
        return "background"
    return None


def _part_bucket(part: dict[str, Any], message_bucket: str | None) -> str | None:
    roles = part.get("temporal_roles", ())
    if "later_progression" in roles:
        return None
    if "in_range_work" in roles:
        return "activity"
    if "earlier_background" in roles:
        return "background"
    return message_bucket if part.get("type") == "text" else None


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
        message_bucket = _message_bucket(message)
        role = message.get("role")
        role = role.lower() if isinstance(role, str) else ""
        if message_bucket != bucket or role not in {"user", "assistant"}:
            continue
        texts: list[str] = []
        for part in message.get("parts", ()):
            if (
                not isinstance(part, dict)
                or _part_bucket(part, message_bucket) != bucket
                or part.get("type") != "text"
            ):
                continue
            value = _part_value(part)
            text = value.get("text", part.get("text"))
            if isinstance(text, str) and text:
                texts.append(text)
        messages.append((message, role, texts))

    if bucket == "background":
        user_positions = [
            index for index, (_, role, _) in enumerate(messages) if role == "user"
        ]
        if len(user_positions) > _BACKGROUND_USER_TURN_LIMIT:
            retained_users = set(user_positions[-_BACKGROUND_USER_TURN_LIMIT:])
            retained: list[tuple[dict[str, Any], str, list[str]]] = []
            retain_assistant = False
            for index, entry in enumerate(messages):
                _, role, _ = entry
                if role == "user":
                    retain_assistant = index in retained_users
                if index in retained_users or (
                    role == "assistant" and retain_assistant
                ):
                    retained.append(entry)
            messages = retained

    for message, role, texts in messages:
        if not texts:
            continue
        timestamp = format_timestamp(message.get("created"), timezone)
        lines.extend([_header(str(role), timestamp), ""])
        for text in texts:
            lines.extend([text, ""])
    return lines if len(lines) > _MINIMUM_RENDERED_LINES else []


def render_opencode(
    item: ContextItem, result: ContextExtractionResult
) -> dict[str, list[str]]:
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
    return files


def _paragraphs(lines: list[str]) -> list[tuple[int, int, str]]:
    paragraphs: list[tuple[int, int, str]] = []
    section_start: int | None = None
    for index, line in enumerate([*lines, ""]):
        match = _MESSAGE_HEADER.fullmatch(line)
        if match is not None:
            section_start = index + 1 if match.group(1) == "User" else None
            continue
        if section_start is None:
            continue
        if line.strip():
            continue
        if section_start < index:
            paragraph_lines = lines[section_start:index]
            canonical = "\n".join(line.rstrip() for line in paragraph_lines)
            paragraphs.append((section_start, index, canonical))
        section_start = index + 1
    return paragraphs


def deduplicate_opencode_user_text(staging: Path) -> None:
    """Share repeated long User paragraphs across rendered OpenCode files."""
    occurrences: dict[str, list[tuple[Path, int, int, str]]] = {}
    for path in sorted((staging / "opencode").rglob("*.md")):
        if path.name not in {"activity.md", "background.md"}:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for start, end, canonical in _paragraphs(lines):
            if len(canonical.encode("utf-8")) < _REPEATED_TEXT_MINIMUM_BYTES:
                continue
            occurrences.setdefault(canonical, []).append(
                (path, start, end, "\n".join(lines[start:end]))
            )
    repeated = {
        text: entries
        for text, entries in occurrences.items()
        if len(entries) >= _MINIMUM_DUPLICATE_OCCURRENCES
    }
    if not repeated:
        return

    shared_lines = ["# Repeated User Text", ""]
    digests: dict[str, str] = {}
    for canonical in sorted(repeated):
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
            :_REPEATED_TEXT_DIGEST_LENGTH
        ]
        digests[canonical] = digest
        shared_lines.extend([f"## sha256-{digest}", ""])
        shared_lines.extend(repeated[canonical][0][3].splitlines())
        shared_lines.append("")

    shared = staging / "opencode" / "_shared" / "repeated-user-text.md"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("\n".join(shared_lines).rstrip() + "\n", encoding="utf-8")

    for path in sorted(
        {entry[0] for entries in repeated.values() for entry in entries}
    ):
        lines = path.read_text(encoding="utf-8").splitlines()
        replacements: list[tuple[int, int, str]] = []
        for start, end, canonical in _paragraphs(lines):
            if canonical not in repeated:
                continue
            relative = path.relative_to(staging / "opencode")
            prefix = "../" * len(relative.parent.parts)
            reference = (
                f"[repeated User text]({prefix}_shared/repeated-user-text.md"
                f"#sha256-{digests[canonical]})"
            )
            replacements.append((start, end, reference))
        for start, end, replacement in reversed(replacements):
            lines[start:end] = [replacement]
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
