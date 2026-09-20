"""Offline Context Output extraction from the published Raw Archive."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .archive import (
    ArchiveError,
    PublishedRun,
    PublishedSnapshot,
    load_published_archive,
    load_published_context_archive,
)
from .context_adapter import ContextAdapter
from .context_adapters import CONTEXT_ADAPTERS, adapter_for

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|z|[+-]\d{2}:\d{2})$"
)


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
    snapshot: PublishedSnapshot
    adapter: ContextAdapter
    projection: object
    path: str

    @property
    def source_kind(self) -> str:
        return self.adapter.source_kind


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


def _load_context_archive(
    root: str | Path, request: ContextRequest
) -> tuple[PublishedRun, ...]:
    try:
        return load_published_context_archive(root, request.start)
    except ArchiveError as error:
        raise ContextError(str(error)) from None


def _logical_key(snapshot: PublishedSnapshot) -> tuple[str, str, str, str]:
    return (
        snapshot.manifest["source_kind"],
        snapshot.run["source"]["scope_id"],
        snapshot.manifest["object_kind"],
        snapshot.manifest["source_id"],
    )


def _observation_window(snapshot: PublishedSnapshot) -> tuple[datetime, datetime]:
    window = snapshot.manifest.get("observation_window")
    if not isinstance(window, dict):
        raise ContextError("snapshot observation window is invalid")
    start_value = window.get("from")
    end_value = window.get("to")
    if not isinstance(start_value, str) or not isinstance(end_value, str):
        raise ContextError("snapshot observation window is invalid")
    parsed: list[datetime] = []
    for value in (start_value, end_value):
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            timestamp = datetime.fromisoformat(normalized)
        except ValueError:
            raise ContextError("snapshot observation window is invalid") from None
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ContextError("snapshot observation window is invalid")
        parsed.append(timestamp)
    start, end = parsed
    if start >= end:
        raise ContextError("snapshot observation window is invalid")
    return start, end


def _observation_sort_key(
    snapshot: PublishedSnapshot,
) -> tuple[datetime, datetime, str]:
    start, end = _observation_window(snapshot)
    return end, start, snapshot.run["run_id"]


def select_observation(
    snapshots: tuple[PublishedSnapshot, ...], end: datetime
) -> PublishedSnapshot:
    """Select one observation using only its manifest window and run identity."""
    if not snapshots:
        raise ContextError("logical object has no snapshot")
    ordered = sorted(snapshots, key=_observation_sort_key)
    at_or_after_request_end = [
        snapshot for snapshot in ordered if _observation_window(snapshot)[1] >= end
    ]
    return at_or_after_request_end[0] if at_or_after_request_end else ordered[-1]


def extract_context(
    request: ContextRequest,
    runs: tuple[PublishedRun, ...],
    archive_root: str | Path | None = None,
) -> ContextExtractionResult:
    """Group archive objects, select observations, and invoke source adapters."""
    grouped: dict[tuple[str, str, str, str], list[PublishedSnapshot]] = {}
    for run in runs:
        for snapshot in run.snapshots:
            adapter = adapter_for(snapshot.manifest["source_kind"])
            if (
                adapter is None
                or snapshot.manifest["object_kind"] not in adapter.object_kinds
            ):
                raise ContextError("unsupported context source")
            grouped.setdefault(_logical_key(snapshot), []).append(snapshot)
    all_items: list[ContextItem] = []
    for key, snapshots in sorted(grouped.items()):
        selected_snapshot = select_observation(tuple(snapshots), request.end)
        adapter = adapter_for(key[0])
        if adapter is None:
            raise ContextError("unsupported context source")
        try:
            projection = adapter.project(selected_snapshot, request.start, request.end)
        except ArchiveError as error:
            raise ContextError(str(error)) from None
        if projection is None or not adapter.include(projection):
            continue
        all_items.append(
            ContextItem(
                snapshot=selected_snapshot,
                adapter=adapter,
                projection=projection,
                path="",
            )
        )
    for adapter in CONTEXT_ADAPTERS:
        positions = [
            index for index, item in enumerate(all_items) if item.adapter is adapter
        ]
        if not positions:
            continue
        try:
            prepared = adapter.prepare(
                tuple(all_items[index] for index in positions), runs, archive_root
            )
        except ArchiveError as error:
            raise ContextError(str(error)) from None
        if len(prepared) != len(positions):
            raise ContextError("context adapter returned an invalid item set")
        for index, item in zip(positions, prepared, strict=True):
            all_items[index] = item
    return ContextExtractionResult(request, runs, tuple(all_items))


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    # Import lazily to keep extraction independent from renderer modules.
    from .context_render import render_context  # noqa: PLC0415

    return render_context(
        extract_context(
            request,
            _load_context_archive(archive, request),
            str(Path(archive)),
        ),
        output,
    )
