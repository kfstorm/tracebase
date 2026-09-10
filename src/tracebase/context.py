"""Offline Context Output extraction from the published Raw Archive."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive import (
    ArchiveError,
    PublishedRun,
    PublishedSnapshot,
    load_published_archive,
)
from .github_context import GitHubProjection, project_github
from .opencode_context import (
    OpenCodeProjection,
    project_opencode,
)

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|z|[+-]\d{2}:\d{2})$"
)
_SUPPORTED_CONTEXT_OBJECT_KINDS = {
    "github": {"issue", "pull-request"},
    "opencode": {"session"},
}


class ContextError(ArchiveError):
    """Raised when context generation cannot satisfy its public contract."""


@dataclass(frozen=True, slots=True)
class ContextRequest:
    from_text: str
    to_text: str
    start: datetime
    end: datetime

    @classmethod
    def parse(cls, from_text: str, to_text: str) -> ContextRequest:
        start = cls._endpoint(from_text)
        end = cls._endpoint(to_text)
        if start >= end:
            raise ContextError("context range requires from to be before to")
        return cls(from_text, to_text, start, end)

    @staticmethod
    def _endpoint(value: str) -> datetime:
        if (
            isinstance(value, str)
            and "T" in value
            and not re.search(r"(?:Z|z|[+-]\d{2}:\d{2})$", value)
        ):
            raise ContextError("context endpoints require an explicit offset")
        if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
            raise ContextError(
                "context endpoints require ISO 8601 offsets and 0-6 fractional digits"
            )
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError:
            raise ContextError(
                "context endpoint is not a valid ISO 8601 timestamp"
            ) from None
        if result.tzinfo is None or result.utcoffset() is None:
            raise ContextError("context endpoints require an explicit offset")
        return result


@dataclass(frozen=True, slots=True)
class ContextItem:
    snapshots: tuple[PublishedSnapshot, ...]
    path: str
    github: GitHubProjection | None = None
    opencode: OpenCodeProjection | None = None

    @property
    def source(self) -> PublishedSnapshot:
        return self.snapshots[0]


@dataclass(frozen=True, slots=True)
class ContextExtractionResult:
    request: ContextRequest
    runs: tuple[PublishedRun, ...]
    items: tuple[ContextItem, ...]


def load_archive(root: str | Path) -> tuple[PublishedRun, ...]:
    """Load the current Raw Archive while the caller keeps it immutable."""
    try:
        return load_published_archive(root)
    except ArchiveError as error:
        raise ContextError(str(error)) from None


def _logical_key(snapshot: PublishedSnapshot) -> tuple[str, str, str, str]:
    return (
        snapshot.manifest["source_kind"],
        snapshot.run["source"]["scope_id"],
        snapshot.manifest["object_kind"],
        snapshot.manifest["source_id"],
    )


def _snapshot_sort_key(snapshot: PublishedSnapshot) -> tuple[datetime, datetime, str]:
    collection_range = snapshot.run["collection_range"]

    def parse(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        return datetime.fromisoformat(normalized)

    return (
        parse(collection_range["from"]),
        parse(collection_range["to"]),
        snapshot.run["run_id"],
    )


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return component if component not in {"", ".", ".."} else "unknown"


def _github_path(projection: GitHubProjection) -> str:
    owner, _, repository = projection.repository.partition("/")
    owner = _safe_component(owner)
    repository = _safe_component(repository)
    kind = (
        "pull"
        if any(record.get("kind") == "pull-request" for record in projection.records)
        else "issue"
    )
    return f"github/{owner}/{repository}/{kind}/{projection.number}"


def _session_parts(
    projection: OpenCodeProjection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    return value, info


def _session_directory(projection: OpenCodeProjection) -> str:
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
    safe_parts = [_safe_component(part) for part in parts if part not in {"", "."}]
    if not safe_parts:
        safe_parts = ["root"]
    return f"opencode/{'/'.join(safe_parts)}/session/{session_number:02d}"


def _session_first_in_range(projection: OpenCodeProjection) -> datetime:
    candidates: list[datetime] = []
    for message in projection.messages:
        if "in_range_work" in message.get("temporal_roles", ()):
            created = message.get("created")
            if isinstance(created, str):
                with suppress(ValueError):
                    candidates.append(datetime.fromisoformat(created))
        for part in message.get("parts", ()):
            if "in_range_work" not in part.get("temporal_roles", ()):
                continue
            started = part.get("start")
            if isinstance(started, str):
                with suppress(ValueError):
                    candidates.append(datetime.fromisoformat(started))
    return min(candidates) if candidates else datetime.max.replace(tzinfo=UTC)


def _validate_context_source(snapshot: PublishedSnapshot) -> None:
    source_kind = snapshot.manifest["source_kind"]
    supported_kinds = _SUPPORTED_CONTEXT_OBJECT_KINDS.get(source_kind)
    if (
        supported_kinds is None
        or snapshot.manifest["object_kind"] not in supported_kinds
    ):
        raise ContextError("unsupported context source")


def extract_context(
    request: ContextRequest, runs: tuple[PublishedRun, ...]
) -> ContextExtractionResult:
    """Group archive objects and run source-specific extraction."""
    grouped: dict[tuple[str, str, str, str], list[PublishedSnapshot]] = {}
    for run in runs:
        for snapshot in run.snapshots:
            _validate_context_source(snapshot)
            grouped.setdefault(_logical_key(snapshot), []).append(snapshot)
    all_items: list[ContextItem] = []
    opencode_projections: dict[tuple[str, str, str, str], OpenCodeProjection] = {}
    for key, snapshots in sorted(grouped.items()):
        ordered = tuple(sorted(snapshots, key=_snapshot_sort_key))
        try:
            github = (
                project_github(ordered, request.start, request.end)
                if key[0] == "github"
                else None
            )
            opencode = (
                project_opencode(ordered, request.start, request.end)
                if key[0] == "opencode"
                else None
            )
        except ArchiveError as error:
            raise ContextError(str(error)) from None
        if opencode is not None:
            opencode_projections[key] = opencode
        all_items.append(ContextItem(ordered, "", github, opencode))
    # Child sessions are evidence consumed by a root task, not independent
    # user-facing documents. The parent task output is the semantic boundary.
    all_items = [
        replace(
            item,
            opencode=opencode_projections.get(_logical_key(item.source)),
        )
        for item in all_items
    ]
    all_items = [
        item
        for item in all_items
        if (item.github is None or item.github.selected)
        and (
            item.opencode is None
            or (item.opencode.selected and item.opencode.parent_id is None)
        )
    ]
    github_items = [item for item in all_items if item.github is not None]
    for item in github_items:
        assert item.github is not None
        item_index = all_items.index(item)
        all_items[item_index] = replace(item, path=_github_path(item.github))
    opencode_items = [item for item in all_items if item.opencode is not None]
    grouped_sessions: dict[str, list[ContextItem]] = {}
    for item in opencode_items:
        assert item.opencode is not None
        grouped_sessions.setdefault(_session_directory(item.opencode), []).append(item)
    for directory, grouped_items in grouped_sessions.items():
        ordered_items = sorted(
            grouped_items,
            key=lambda item: (
                _session_first_in_range(item.opencode)
                if item.opencode is not None
                else datetime.max,
                _session_title(item.opencode) if item.opencode is not None else "",
                item.opencode.session_id if item.opencode is not None else "",
            ),
        )
        for session_number, item in enumerate(ordered_items, start=1):
            item_index = all_items.index(item)
            all_items[item_index] = replace(
                item, path=_opencode_path(directory, session_number)
            )
    return ContextExtractionResult(request, runs, tuple(all_items))


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    # Import lazily to keep extraction independent from renderer modules.
    from .context_render import render_context  # noqa: PLC0415

    return render_context(extract_context(request, load_archive(archive)), output)
