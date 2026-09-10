"""Consumer-oriented OpenCode Context Output rendering."""

from __future__ import annotations

import json
import re
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import fenced, format_timestamp

_LOW_VALUE_TOOLS = {
    "read",
    "grep",
    "glob",
    "skill",
    "todowrite",
    "webfetch",
    "websearch",
    "openchamber",
    "openchamber_web",
}
_PATCH_MARKER = re.compile(
    r"^\*\*\* (Add File|Update File|Delete File|Move to):\s*(.+?)\s*$"
)
_EMPTY_DOCUMENT_LINES = 2


def _part_value(part: dict[str, Any]) -> dict[str, Any]:
    value = part.get("value")
    return value if isinstance(value, dict) else part


def _observation_end(part: dict[str, Any]) -> datetime | None:
    window = part.get("observation_window")
    if not isinstance(window, dict) or not isinstance(window.get("to"), str):
        return None
    try:
        parsed = datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _effective_part(part: dict[str, Any], end: datetime) -> dict[str, Any]:
    representations = part.get("representations")
    representations = representations if isinstance(representations, list) else []
    eligible = [
        representation
        for representation in representations
        if isinstance(representation, dict)
        and (observed_end := _observation_end(representation)) is not None
        and observed_end <= end
        and isinstance(representation.get("value"), dict)
    ]
    if eligible:
        return {**part, "value": eligible[-1]["value"]}
    value = _part_value(part)
    safe_state = _state(part).copy()
    safe_state.pop("output", None)
    safe_state.pop("error", None)
    metadata = safe_state.get("metadata")
    if isinstance(metadata, dict):
        safe_state["metadata"] = {
            key: value for key, value in metadata.items() if key != "answers"
        }
    safe_state["status"] = "running"
    time_data = safe_state.get("time")
    if isinstance(time_data, dict):
        safe_state["time"] = {
            key: value for key, value in time_data.items() if key != "end"
        }
    return {**part, "value": {**value, "state": safe_state}}


def _state(part: dict[str, Any]) -> dict[str, Any]:
    state = _part_value(part).get("state")
    return state if isinstance(state, dict) else {}


def _tool_name(part: dict[str, Any]) -> str:
    value = _part_value(part)
    tool = value.get("tool", part.get("type"))
    return tool if isinstance(tool, str) else "unknown"


def _tool_input(part: dict[str, Any]) -> Any:
    value = _part_value(part)
    state = _state(part)
    return state.get("input", value.get("input"))


def _error(part: dict[str, Any]) -> str | None:
    state = _state(part)
    status = state.get("status")
    error = state.get("error", _part_value(part).get("error"))
    if status == "error" or error is not None:
        if isinstance(error, str):
            return error
        return json.dumps(error, ensure_ascii=False, sort_keys=True)
    return None


def _path(value: Any, directory: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = Path(value)
        root = Path(directory)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve(strict=False).relative_to(
                    root.resolve(strict=False)
                )
            except ValueError:
                return value
            return f"./{relative.as_posix()}"
    except ValueError:
        return value
    return value


def _workdir(value: Any, directory: str) -> str | None:
    if isinstance(value, str) and value == directory:
        return None
    rendered = _path(value, directory)
    return None if rendered in {None, ".", directory} else rendered


def _patch_paths(patch: Any) -> list[str]:
    if not isinstance(patch, str):
        return []
    paths: list[str] = []
    for line in patch.splitlines():
        match = _PATCH_MARKER.match(line)
        if match is not None and match.group(2) not in paths:
            paths.append(match.group(2))
    return paths


def _question_text(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("questions", value.get("question"))
    return value


def _answers(part: dict[str, Any]) -> Any:
    metadata = _state(part).get("metadata")
    return metadata.get("answers") if isinstance(metadata, dict) else None


def _task_output(part: dict[str, Any]) -> Any:
    output = _state(part).get("output")
    if not isinstance(output, str):
        return output
    match = re.fullmatch(
        r"<task\b[^>]*>\s*<task_result>\s*(.*?)\s*</task_result>\s*</task>\s*",
        output,
        flags=re.DOTALL,
    )
    return match.group(1) if match is not None else output


def _task_description(part: dict[str, Any]) -> str | None:
    value = _part_value(part)
    description = value.get("description")
    return description if isinstance(description, str) and description else None


def _completed_before_cutoff(part: dict[str, Any], end: datetime) -> bool:
    value = part.get("end")
    if not isinstance(value, str):
        return False
    try:
        completed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return completed < end


def _message_bucket(
    message: dict[str, Any], start: datetime, end: datetime
) -> str | None:
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


def _render_error(lines: list[str], tool: str, error: str) -> None:
    lines.extend([f"### {tool} failed", "", error, ""])


def _render_tool(  # noqa: PLR0911, PLR0915
    lines: list[str],
    part: dict[str, Any],
    directory: str,
    timezone: tzinfo,
    end: datetime,
) -> None:
    part = _effective_part(part, end)
    tool = _tool_name(part)
    error = _error(part)
    if error is not None and not _completed_before_cutoff(part, end):
        error = None
    if error is not None:
        _render_error(lines, tool, error)
        return
    state = _state(part)
    if tool == "task":
        description = _task_description(part)
        lines.extend(
            [f"### Delegated task{f': {description}' if description else ''}", ""]
        )
        status = state.get("status")
        completed_before_cutoff = _completed_before_cutoff(part, end)
        if status in {"running", "pending"} or not completed_before_cutoff:
            lines.extend(["Incomplete task.", ""])
        output = _task_output(part) if completed_before_cutoff else None
        if output is not None:
            rendered = (
                output
                if isinstance(output, str)
                else json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True)
            )
            lines.extend(
                [
                    "Result:",
                    "",
                    *fenced(rendered, "text" if isinstance(output, str) else "json"),
                    "",
                ]
            )
        return
    if tool == "question":
        questions = _question_text(_tool_input(part))
        lines.extend(["### Question", ""])
        if questions is not None:
            rendered = (
                questions
                if isinstance(questions, str)
                else json.dumps(questions, ensure_ascii=False, indent=2, sort_keys=True)
            )
            lines.extend(
                fenced(rendered, "text" if isinstance(questions, str) else "json")
            )
            lines.append("")
        answers = _answers(part) if _completed_before_cutoff(part, end) else None
        if answers is not None:
            rendered = (
                answers
                if isinstance(answers, str)
                else json.dumps(answers, ensure_ascii=False, indent=2, sort_keys=True)
            )
            lines.extend(
                [
                    "Human answer:",
                    "",
                    *fenced(rendered, "text" if isinstance(answers, str) else "json"),
                    "",
                ]
            )
        return
    tool_input = _tool_input(part)
    if tool == "apply_patch":
        paths = _patch_paths(
            tool_input.get("patchText") if isinstance(tool_input, dict) else None
        )
        if paths:
            lines.extend(
                [
                    "### Changed files",
                    "",
                    *[f"- {_path(path, directory)}" for path in paths],
                    "",
                ]
            )
        return
    if tool in {"edit", "write"}:
        file_path = tool_input.get("filePath") if isinstance(tool_input, dict) else None
        file_path = file_path or (
            tool_input.get("path") if isinstance(tool_input, dict) else None
        )
        rendered_path = _path(file_path, directory)
        if rendered_path:
            lines.extend([f"### {tool}", "", f"File: `{rendered_path}`", ""])
        return
    if tool == "bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
        lines.extend(["### bash", ""])
        if isinstance(command, str):
            lines.extend(fenced(command, "bash"))
        rendered_workdir = _workdir(workdir, directory)
        if rendered_workdir:
            lines.extend(["", f"Workdir: `{rendered_workdir}`"])
        lines.append("")
        return
    if tool in _LOW_VALUE_TOOLS:
        return
    # Unknown successful tools are intentionally omitted.


def _render_messages(
    item: ContextItem, result: ContextExtractionResult, bucket: str
) -> list[str]:
    assert item.opencode is not None
    projection = item.opencode
    timezone = result.request.start.tzinfo
    assert timezone is not None
    directory = projection.session.get("working_directory")
    directory = directory if isinstance(directory, str) else "."
    lines = [f"# {'Activity' if bucket == 'activity' else 'Background'}", ""]
    for message in projection.messages:
        message_bucket = _message_bucket(
            message, result.request.start, result.request.end
        )
        if message_bucket != bucket:
            continue
        role = (
            message.get("role") if isinstance(message.get("role"), str) else "message"
        )
        timestamp = format_timestamp(message.get("created"), timezone)
        lines.extend([_header(str(role), timestamp), ""])
        for part in message.get("parts", ()):
            if (
                not isinstance(part, dict)
                or _part_bucket(part, message_bucket) != bucket
            ):
                continue
            part_type = part.get("type")
            effective_part = _effective_part(part, result.request.end)
            value = _part_value(effective_part)
            if part_type == "text":
                text = value.get("text", part.get("text"))
                if isinstance(text, str) and text:
                    lines.extend([text, ""])
            elif (
                part_type in {"tool", "task", "question"}
                or _tool_name(part) == "question"
            ):
                _render_tool(
                    lines, effective_part, directory, timezone, result.request.end
                )
            elif part_type == "compaction" or value.get("synthetic") is True:
                lines.extend(
                    ["Supporting context retained from an earlier compaction.", ""]
                )
    return [] if lines[-1] == "" and len(lines) == _EMPTY_DOCUMENT_LINES else lines


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
    directory = projection.session.get("working_directory")
    directory = directory if isinstance(directory, str) else "."
    files: dict[str, list[str]] = {
        "overview.md": [f"# {title}", "", f"Working directory: `{directory}`", ""]
    }
    activity = _render_messages(item, result, "activity")
    background = _render_messages(item, result, "background")
    if activity:
        files["activity.md"] = activity
    if background:
        files["background.md"] = background
    return files
