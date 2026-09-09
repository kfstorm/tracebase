"""Offline Context Output extraction from the published Raw Archive."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .archive import (
    ArchiveError,
    PublishedRun,
    PublishedSnapshot,
    encode_path_id,
    load_published_archive,
)
from .github_context import GitHubProjection, project_github
from .opencode_context import (
    OpenCodeProjection,
    project_opencode,
    resolve_opencode_context,
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


def _item_path(key: tuple[str, str, str, str]) -> str:
    return f"{key[0]}/{encode_path_id(key[1])}/{key[2]}/{encode_path_id(key[3])}"


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
        all_items.append(ContextItem(ordered, _item_path(key), github, opencode))
    projections_by_scope: dict[
        str, list[tuple[tuple[str, str, str, str], OpenCodeProjection]]
    ] = {}
    for key, projection in opencode_projections.items():
        projections_by_scope.setdefault(key[1], []).append((key, projection))
    for scoped in projections_by_scope.values():
        resolved = resolve_opencode_context(
            tuple(projection for _, projection in scoped)
        )
        for (key, _), projection in zip(scoped, resolved, strict=True):
            opencode_projections[key] = projection
    supporting_keys = {
        key
        for key, projection in opencode_projections.items()
        if projection.inclusion_reasons == ("supporting-task-context",)
    }
    all_items = [
        replace(
            item,
            opencode=opencode_projections.get(_logical_key(item.source)),
        )
        for item in all_items
    ]
    items = tuple(
        item
        for item in all_items
        if (item.github is None or item.github.selected)
        and (
            item.opencode is None
            or item.opencode.selected
            or _logical_key(item.source) in supporting_keys
        )
    )
    return ContextExtractionResult(request, runs, items)


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    # Import lazily to keep extraction independent from renderer modules.
    from .context_render import render_context  # noqa: PLC0415

    return render_context(extract_context(request, load_archive(archive)), output)
