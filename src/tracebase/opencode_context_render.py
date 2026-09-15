"""Consumer-oriented OpenCode Context Output rendering."""

from __future__ import annotations

from pathlib import Path

from .context import ContextExtractionResult, ContextItem
from .dialogue import render_dialogue_files, write_dialogue_markdown
from .opencode_context import OpenCodeProjection


def _session_context(projection: OpenCodeProjection) -> dict[str, str]:
    context: dict[str, str] = {}
    working_directory = projection.session.get("working_directory")
    if isinstance(working_directory, str) and working_directory:
        context["working_directory"] = working_directory
    project_directory = projection.session.get("project_directory")
    if isinstance(project_directory, str) and project_directory:
        context["project_directory"] = project_directory
    if "project_directory" not in context and "working_directory" in context:
        context["project_directory"] = context["working_directory"]
    return context


def render_opencode(
    item: ContextItem, result: ContextExtractionResult, output: Path
) -> list[str]:
    projection = item.projection
    assert isinstance(projection, OpenCodeProjection)
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    title = info.get("title")
    title = title if isinstance(title, str) and title else "Untitled session"
    context = _session_context(projection)
    directory = context.get("working_directory", ".")
    project_directory = context.get("project_directory", directory)
    overview = [
        f"# {title}",
        "",
        f"Project directory: `{project_directory}`",
    ]
    if directory != project_directory:
        overview.append(f"Working directory: `{directory}`")
    overview.extend(
        [
            "",
            "## Attribution",
            "",
            f"- Attribution mode: `{item.attribution_mode.value}`",
            "- All materially meaningful OpenCode work is user work, including "
            "delegated agent or subagent investigation, design, implementation, "
            "debugging, validation, and decisions.",
            "",
        ]
    )
    timezone = result.request.start.tzinfo
    assert timezone is not None
    files = render_dialogue_files(
        projection.dialogue,
        timezone,
        item.attribution_mode.value,
        overview,
    )
    for name, lines in files.items():
        write_dialogue_markdown(output / name, lines)
    return list(files)
