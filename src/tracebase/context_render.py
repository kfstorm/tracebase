"""Shared deterministic Markdown rendering and publication."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .context import ContextError, ContextExtractionResult, ContextItem

_OPENCODE_GAP_FIELDS: dict[str, tuple[str, ...]] = {
    "malformed-session-parent": ("session_id",),
    "missing-session-parent": ("session_id", "parent_id"),
    "cyclic-session-parent": ("session_id", "parent_id"),
    "malformed-task-child": ("session_id",),
    "missing-task-child": ("session_id", "child_id"),
    "unknown-completion": ("message_id", "part_id"),
    "no_in_range_messages": ("reason",),
}


def public_gap(gap: dict[str, Any]) -> dict[str, str]:
    """Select stable, useful fields for one OpenCode gap."""
    kind = gap.get("kind")
    kind = kind if isinstance(kind, str) else "unknown"
    public = {"kind": kind}
    for field in _OPENCODE_GAP_FIELDS.get(kind, ()):
        value = gap.get(field)
        if isinstance(value, str):
            public[field] = value
    return public


def write_markdown(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def format_timestamp(value: Any, timezone: Any) -> str | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone).strftime("%Y-%m-%d %H:%M")


def _root_gaps(items: tuple[ContextItem, ...]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    for item in items:
        if item.opencode is None:
            continue
        gaps.extend(public_gap(gap) for gap in item.opencode.gaps)
    return sorted(gaps, key=lambda entry: json.dumps(entry, sort_keys=True))


def _render_item(
    staging: Path, item: ContextItem, result: ContextExtractionResult
) -> tuple[
    list[tuple[str, str]],
    dict[str, list[str] | bytes],
    Path,
]:
    item_root = staging.joinpath(*PurePosixPath(item.path).parts)
    item_root.mkdir(parents=True, exist_ok=True)
    files: dict[str, list[str] | bytes] = {}
    if item.github is not None:
        from .github_context_render import render_github  # noqa: PLC0415

        files = render_github(item, result)
    elif item.opencode is not None:
        from .opencode_context_render import render_opencode  # noqa: PLC0415

        files = cast(dict[str, list[str] | bytes], render_opencode(item, result))
    else:
        raise ContextError("context item has no source projection")
    rendered = [(f"{item.path}/{name}", name) for name in files]
    return rendered, files, item_root


def _link_list(paths: list[str]) -> str:
    return ", ".join(f"[{PurePosixPath(path).name}]({path})" for path in paths)


def _session_span(item: ContextItem, result: ContextExtractionResult) -> str | None:
    assert item.opencode is not None
    times: list[datetime] = []
    for message in item.opencode.messages:
        if "in_range_work" in message.get("temporal_roles", ()):
            created = parse_timestamp(message.get("created"))
            if created is not None:
                times.append(created)
        for part in message.get("parts", ()):
            if "in_range_work" not in part.get("temporal_roles", ()):
                continue
            started = parse_timestamp(part.get("start"))
            if started is not None:
                times.append(started)
            finished = parse_timestamp(part.get("end"))
            if finished is not None and finished < result.request.end:
                times.append(finished)
    if not times:
        return None
    timezone = result.request.start.tzinfo
    assert timezone is not None
    first = min(times).astimezone(timezone).strftime("%H:%M")
    last = max(times).astimezone(timezone).strftime("%H:%M")
    return first if first == last else f"{first}-{last}"


def _render_index(
    staging: Path,
    result: ContextExtractionResult,
    items: list[tuple[ContextItem, list[tuple[str, str]]]],
) -> None:
    lines = [
        "# Context Output",
        "",
        "Requested interval: "
        f"`{result.request.from_text} <= t < {result.request.to_text}`",
        "Times are displayed in the requested interval offset.",
        "",
        "## GitHub",
        "",
    ]
    github_items = [(item, files) for item, files in items if item.github is not None]
    tracked_logins = sorted(
        {
            item.github.tracked_login
            for item, _ in github_items
            if item.github is not None and item.github.tracked_login is not None
        }
    )
    for login in tracked_logins:
        lines.extend([f"Tracked GitHub account: @{login}", ""])
    if not github_items:
        lines.append("No GitHub items are available.")
    for item, files in github_items:
        assert item.github is not None
        projection = item.github
        kind = (
            "PR"
            if any(
                record.get("kind") == "pull-request" for record in projection.records
            )
            else "Issue"
        )
        title = projection.title or "Untitled"
        links = [path for path, _ in files]
        lines.append(
            f"- **{projection.repository} {kind} #{projection.number}**: {title} "
            f"({_link_list(links)})"
        )
    lines.extend(["", "## OpenCode", ""])
    opencode_items = [
        (item, files) for item, files in items if item.opencode is not None
    ]
    if not opencode_items:
        lines.append("No OpenCode root sessions are available.")
    grouped: dict[str, list[tuple[ContextItem, list[tuple[str, str]]]]] = {}
    for item, files in opencode_items:
        assert item.opencode is not None
        value = item.opencode.session.get("project_directory", ".")
        directory = value if isinstance(value, str) else "."
        grouped.setdefault(directory, []).append((item, files))
    for directory in sorted(grouped):
        lines.append(f"### `{directory}`")
        lines.append("")
        ordered_group = sorted(
            grouped[directory],
            key=lambda pair: next(
                path for path, _ in pair[1] if path.endswith("/overview.md")
            ),
        )
        for item, files in ordered_group:
            assert item.opencode is not None
            value = item.opencode.session.get("value")
            value = value if isinstance(value, dict) else {}
            info = value.get("info")
            info = info if isinstance(info, dict) else {}
            title_value = info.get("title")
            title = (
                title_value
                if isinstance(title_value, str) and title_value
                else "Untitled session"
            )
            span = _session_span(item, result) or "No in-range time"
            lines.append(
                f"- **{span} - {title}** ({_link_list([path for path, _ in files])})"
            )
        lines.append("")
    gaps = _root_gaps(result.items)
    if gaps:
        lines.extend(["", "## Gaps", ""])
        lines.extend(f"- `{gap['kind']}`" for gap in gaps)
    write_markdown(staging / "index.md", lines)


def _cleanup_staging(staging: Path) -> None:
    if not staging.exists():
        return
    try:
        shutil.rmtree(staging)
    except OSError:
        raise ContextError("context output cleanup failed") from None


def render_context(result: ContextExtractionResult, output: str | Path) -> Path:
    """Render completely, then publish the caller-owned output with one rename."""
    target = Path(output).absolute()
    staging: Path | None = None
    try:
        try:
            if target.exists() or target.is_symlink():
                raise ContextError("context output already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
            )
        except ContextError:
            raise
        except OSError:
            raise ContextError("context output publication failed") from None
        assert staging is not None
        try:
            rendered: list[tuple[ContextItem, list[tuple[str, str]]]] = []
            for item in result.items:
                item_rendered, item_files, item_root = _render_item(
                    staging, item, result
                )
                for name, content in item_files.items():
                    if isinstance(content, bytes):
                        (item_root / name).write_bytes(content)
                    elif item.opencode is not None:
                        from .opencode_context_render import (  # noqa: PLC0415
                            write_opencode_markdown,
                        )

                        write_opencode_markdown(item_root / name, content)
                    else:
                        write_markdown(item_root / name, content)
                rendered.append((item, item_rendered))
            _render_index(staging, result, rendered)
        except ContextError:
            raise
        except KeyError, TypeError, UnicodeError, ValueError, OSError:
            raise ContextError("context output rendering failed") from None
        try:
            staging.rename(target)
        except FileExistsError:
            raise ContextError("context output already exists") from None
        except OSError:
            raise ContextError("context output publication failed") from None
        return target
    finally:
        if staging is not None:
            _cleanup_staging(staging)
