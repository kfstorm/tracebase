"""OpenCode whitelist projection rendering as deterministic Markdown."""

from __future__ import annotations

import json
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import fenced, item_front_matter, public_gap, render_provenance


def _session_context(item: ContextItem) -> dict[str, str]:
    projection = item.opencode
    if projection is None:
        return {}
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    context: dict[str, str] = {}
    for output_key, keys in (
        ("working_directory", ("directory",)),
        (
            "main_worktree_directory",
            ("worktree", "worktreeDirectory", "mainWorktree"),
        ),
    ):
        for source in (info, value):
            for key in keys:
                candidate = source.get(key)
                if isinstance(candidate, str) and candidate:
                    context[output_key] = candidate
                    break
            if output_key in context:
                break
    for snapshot in reversed(item.snapshots):
        metadata = snapshot.manifest.get("metadata")
        session = metadata.get("session") if isinstance(metadata, dict) else None
        if not isinstance(session, dict):
            continue
        for key, output_key in (
            ("directory", "working_directory"),
            ("worktree", "main_worktree_directory"),
        ):
            candidate = session.get(key)
            if isinstance(candidate, str) and candidate and output_key not in context:
                context[output_key] = candidate
    return context


def _part_value(part: dict[str, Any]) -> dict[str, Any]:
    value = part.get("value")
    return value if isinstance(value, dict) else {}


def render_opencode(  # noqa: PLR0915
    item: ContextItem, result: ContextExtractionResult
) -> list[str]:
    assert item.opencode is not None
    projection = item.opencode
    context = _session_context(item)
    lines = item_front_matter(item, result, "opencode", projection)
    lines.extend(["# OpenCode Session", ""])
    if context:
        lines.extend(["## Session Context", ""])
        labels = {
            "working_directory": "Working directory",
            "main_worktree_directory": "Main worktree directory",
        }
        for key in ("working_directory", "main_worktree_directory"):
            if key in context:
                lines.append(f"- {labels[key]}: `{context[key]}`")
        lines.append("")
    render_provenance(lines, item)
    for message in projection.messages:
        role_value = message.get("role")
        role = str(role_value) if isinstance(role_value, str) else "unknown"
        created = message.get("created", "unknown")
        roles = ", ".join(message.get("temporal_roles", ())) or "observed_state"
        lines.extend(
            [
                f"## {role.title()} - {created}",
                "",
                f"Message: `{message['id']}`",
                f"Temporal roles: {roles}",
                "",
            ]
        )
        for part in message.get("parts", ()):
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            raw = _part_value(part)
            part_id = part.get("id", "unknown")
            part_roles = ", ".join(part.get("temporal_roles", ())) or "observed_state"
            if part_type == "text":
                text = raw.get("text", part.get("text"))
                if isinstance(text, str):
                    lines.extend(
                        [
                            f"### Text - `{part_id}`",
                            "",
                            f"Temporal roles: {part_roles}",
                            "",
                        ]
                    )
                    lines.extend(fenced(text, "text"))
                    lines.append("")
            elif part_type in {"tool", "task"}:
                state = raw.get("state")
                state = state if isinstance(state, dict) else {}
                tool_name = raw.get("tool", part_type)
                tool_name = tool_name if isinstance(tool_name, str) else part_type
                lines.extend(
                    [
                        f"### Tool - {tool_name} - `{part_id}`",
                        "",
                        f"Temporal roles: {part_roles}",
                    ]
                )
                status = state.get("status")
                if isinstance(status, str):
                    lines.append(f"Status: {status}")
                if isinstance(part.get("start"), str):
                    lines.append(f"Start: {part['start']}")
                if isinstance(part.get("end"), str):
                    lines.append(f"End: {part['end']}")
                elif part.get("completion") == "unknown":
                    lines.append("End: unknown")
                metadata = state.get("metadata")
                if isinstance(metadata, dict):
                    relationship = {
                        key: metadata[key]
                        for key in ("parentSessionId", "sessionId")
                        if isinstance(metadata.get(key), str)
                    }
                    if relationship:
                        lines.append(
                            "Task relationship: "
                            f"`{json.dumps(relationship, sort_keys=True)}`"
                        )
                tool_input = state.get("input", raw.get("input"))
                if tool_input is not None:
                    lines.extend(["", "#### Input", ""])
                    rendered_input = json.dumps(
                        tool_input,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    lines.extend(fenced(rendered_input, "json"))
                lines.append("")
            elif part_type == "compaction" or raw.get("synthetic") is True:
                lines.extend(
                    [
                        f"### Supporting context - `{part_id}`",
                        "",
                        "Compaction or synthetic continuation; not independent "
                        "repeated work.",
                        "",
                    ]
                )
                tail_start = raw.get("tail_start_id")
                if isinstance(tail_start, str):
                    lines.append(f"Tail start message: `{tail_start}`")
                    lines.append("")
    if projection.gaps:
        lines.extend(["## Gaps", ""])
        for gap in projection.gaps:
            public = public_gap(gap)
            lines.append(
                f"- `{public['kind']}`: `{json.dumps(public, sort_keys=True)}`"
            )
        lines.append("")
    return lines
