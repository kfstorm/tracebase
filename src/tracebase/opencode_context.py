"""Deterministic OpenCode conversational Context projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .archive import ArchiveError, PublishedRun, PublishedSnapshot
from .attribution import (
    CONVERSATIONAL_ATTRIBUTION_POLICY,
    CONVERSATIONAL_EVIDENCE_POLICY,
)
from .context_adapter import (
    ContextOrdering,
    RenderedContextItem,
    number_context_items,
    render_source_item,
    safe_path_component,
)
from .dialogue import (
    DialogueTranscript,
    DialogueTurn,
    dialogue_sort_key,
    project_dialogue,
)

if TYPE_CHECKING:
    from .context import ContextExtractionResult, ContextItem


@dataclass(frozen=True, slots=True)
class OpenCodeProjection:
    """OpenCode metadata plus its text-only conversational transcript."""

    session: dict[str, Any]
    dialogue: DialogueTranscript

    @property
    def session_id(self) -> str:
        value = self.session.get("value")
        value = value if isinstance(value, dict) else {}
        info = value.get("info")
        identifier = info.get("id") if isinstance(info, dict) else value.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ArchiveError("OpenCode session payload is invalid")
        return identifier

    @property
    def parent_id(self) -> str | None:
        value = self.session.get("value")
        value = value if isinstance(value, dict) else {}
        info = value.get("info")
        parent = info.get("parentID") if isinstance(info, dict) else None
        if parent is None:
            parent = value.get("parentID")
        return parent if isinstance(parent, str) else None


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


def _session_header(value: dict[str, Any]) -> dict[str, Any]:
    """Keep source-specific identity and display metadata, not raw messages."""
    header: dict[str, Any] = {}
    for key in ("id", "directory", "parentID"):
        if key in value:
            header[key] = value[key]
    info = value.get("info")
    if isinstance(info, dict):
        header["info"] = {
            key: info[key]
            for key in ("id", "title", "directory", "parentID")
            if key in info
        }
    return header


def _project_worktree(snapshot: PublishedSnapshot) -> str | None:
    raw_project = snapshot.evidence.get("project.json")
    if raw_project is None:
        return None
    try:
        project = json.loads(raw_project)
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(project, dict) or project.get("id") == "global":
        return None
    worktree = project.get("worktree")
    return (
        worktree if isinstance(worktree, str) and worktree and worktree != "/" else None
    )


def _created(value: dict[str, Any]) -> datetime:
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    time_data = info.get("time", value.get("time"))
    time_data = time_data if isinstance(time_data, dict) else {}
    for candidate in (value.get("created"), time_data.get("created")):
        if isinstance(candidate, (int, float, str)) and not isinstance(candidate, bool):
            return _timestamp(candidate)
    raise ArchiveError("OpenCode message payload is invalid")


def _hidden(value: dict[str, Any]) -> bool:
    info = value.get("info")
    metadata = value.get("metadata")
    return bool(
        value.get("hidden") is True
        or (isinstance(info, dict) and info.get("hidden") is True)
        or (isinstance(metadata, dict) and metadata.get("hidden") is True)
    )


def _supporting_message(value: dict[str, Any]) -> bool:
    info = value.get("info")
    return isinstance(info, dict) and (
        info.get("mode") == "compaction"
        or info.get("summary") is True
        or info.get("agent") == "compaction"
    )


def _text_parts(message: dict[str, Any]) -> str | None:
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise ArchiveError("OpenCode message payload is invalid")
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            raise ArchiveError("OpenCode message payload is invalid")
        if part.get("type") != "text":
            continue
        value = part.get("value")
        value = value if isinstance(value, dict) else part
        metadata = value.get("metadata")
        if (
            part.get("synthetic") is True
            or value.get("synthetic") is True
            or part.get("ignored") is True
            or value.get("ignored") is True
            or (
                isinstance(metadata, dict)
                and metadata.get("compaction_continue") is True
            )
            or value.get("hidden") is True
        ):
            continue
        text = value.get("text", part.get("text"))
        if not isinstance(text, str):
            # A malformed text part is not safely recoverable as dialogue.
            return None
        if text:
            texts.append(text)
    return "\n\n".join(texts) or None


def project_opencode(
    snapshot: PublishedSnapshot, start: datetime, end: datetime
) -> OpenCodeProjection | None:
    """Project only user/assistant text using message creation time."""
    source_id = snapshot.manifest["source_id"]
    payload = _json(snapshot)
    info = payload.get("info")
    info = info if isinstance(info, dict) else {}
    payload_id = payload.get("id")
    info_id = info.get("id")
    if not any(value == source_id for value in (payload_id, info_id)) or any(
        value is not None and value != source_id for value in (payload_id, info_id)
    ):
        raise ArchiveError("OpenCode session payload identity was invalid")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ArchiveError("OpenCode session payload is invalid")

    turns: list[DialogueTurn] = []
    for message in raw_messages:
        if not isinstance(message, dict):
            raise ArchiveError("OpenCode message payload is invalid")
        role = message.get("role")
        message_info = message.get("info")
        if role is None and isinstance(message_info, dict):
            role = message_info.get("role")
        role = role.lower() if isinstance(role, str) else ""
        if role not in {"user", "assistant"}:
            continue
        if _hidden(message) or _supporting_message(message):
            continue
        text = _text_parts(message)
        if text is None:
            continue
        turns.append(DialogueTurn(role, _created(message), text))

    dialogue = project_dialogue(turns, start, end)
    if dialogue is None:
        return None

    latest_value = _session_header(payload)
    session: dict[str, Any] = {"value": latest_value}
    latest_info = latest_value.get("info")
    latest_info = latest_info if isinstance(latest_info, dict) else {}
    directory = latest_info.get("directory", latest_value.get("directory"))
    if not isinstance(directory, str) or not directory:
        metadata = snapshot.manifest.get("metadata")
        session_metadata = (
            metadata.get("session") if isinstance(metadata, dict) else None
        )
        candidate = (
            session_metadata.get("directory")
            if isinstance(session_metadata, dict)
            else None
        )
        if isinstance(candidate, str) and candidate:
            directory = candidate
    if isinstance(directory, str) and directory:
        session["working_directory"] = directory
    project_directory = _project_worktree(snapshot)
    if project_directory is None and isinstance(directory, str) and directory:
        project_directory = directory
    if project_directory is not None:
        session["project_directory"] = project_directory
    return OpenCodeProjection(session, dialogue)


def _session_parts(
    projection: OpenCodeProjection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    return value, info


def _session_directory(projection: OpenCodeProjection) -> str:
    project_directory = projection.session.get("project_directory")
    if isinstance(project_directory, str) and project_directory:
        return project_directory
    value, info = _session_parts(projection)
    directory = info.get("directory", value.get("directory"))
    if isinstance(directory, str) and directory:
        return directory
    directory = projection.session.get("working_directory")
    if isinstance(directory, str) and directory:
        return directory
    return "."


def _session_title(projection: OpenCodeProjection) -> str:
    value, info = _session_parts(projection)
    title = info.get("title", value.get("title"))
    return title if isinstance(title, str) and title else "Untitled session"


def _opencode_path(directory: str, session_number: int) -> str:
    parts = PurePosixPath(directory).parts
    if parts and parts[0] == "/":
        parts = parts[1:]
    safe_parts = [safe_path_component(part) for part in parts if part not in {"", "."}]
    if not safe_parts:
        safe_parts = ["root"]
    return f"opencode/{'/'.join(safe_parts)}/session/{session_number:02d}"


def _session_sort_key(item: ContextItem) -> tuple[datetime, str, str]:
    projection = item.projection
    if not isinstance(projection, OpenCodeProjection):
        raise ArchiveError("OpenCode context projection was invalid")
    return dialogue_sort_key(
        projection.dialogue,
        _session_title(projection),
        projection.session_id,
    )


class OpenCodeContextAdapter:
    source_kind = "opencode"
    object_kinds = frozenset({"session"})
    attribution_policy = CONVERSATIONAL_ATTRIBUTION_POLICY
    evidence_policy = CONVERSATIONAL_EVIDENCE_POLICY

    def project(
        self, snapshot: PublishedSnapshot, start: datetime, end: datetime
    ) -> OpenCodeProjection | None:
        return project_opencode(snapshot, start, end)

    def include(self, projection: object) -> bool:
        if not isinstance(projection, OpenCodeProjection):
            raise ArchiveError("OpenCode context projection was invalid")
        return projection.parent_id is None

    def prepare(
        self,
        items: tuple[ContextItem, ...],
        _runs: tuple[PublishedRun, ...],
        _archive_root: str | Path | None,
    ) -> tuple[ContextItem, ...]:
        grouped: dict[str, list[tuple[int, ContextItem]]] = {}
        for index, item in enumerate(items):
            projection = item.projection
            if not isinstance(projection, OpenCodeProjection):
                raise ArchiveError("OpenCode context projection was invalid")
            grouped.setdefault(_session_directory(projection), []).append((index, item))
        prepared = list(items)
        for directory, grouped_items in grouped.items():
            for index, item in number_context_items(
                tuple(grouped_items),
                partial(_opencode_path, directory),
                _session_sort_key,
            ):
                prepared[index] = item
        return tuple(prepared)

    def render(
        self, item: ContextItem, result: ContextExtractionResult, output: Path
    ) -> RenderedContextItem:
        from .opencode_context_render import render_opencode  # noqa: PLC0415

        return render_source_item(self, item, result, output, render_opencode)

    def ordering_metadata(
        self, item: ContextItem, result: ContextExtractionResult
    ) -> ContextOrdering:
        projection = item.projection
        if not isinstance(projection, OpenCodeProjection):
            raise ArchiveError("OpenCode context projection was invalid")
        timezone = result.request.start.tzinfo
        if timezone is None:
            raise ArchiveError("Context request timezone is invalid")
        return ContextOrdering(
            _session_directory(projection),
            (item.path,),
        )


OPENCODE_CONTEXT_ADAPTER = OpenCodeContextAdapter()
