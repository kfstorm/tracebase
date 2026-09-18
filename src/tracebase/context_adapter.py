"""The source-adapter seam used by offline Context extraction."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .archive import PublishedRun, PublishedSnapshot
from .attribution import AttributionMode

if TYPE_CHECKING:
    from .context import ContextExtractionResult, ContextItem


@dataclass(frozen=True, slots=True)
class ContextIndexEntry:
    """Source-provided values needed by the shared Context index layout."""

    group: str | None
    sort_key: tuple[str, ...]
    label: str


@dataclass(frozen=True, slots=True)
class RenderedContextItem:
    """The source files and index values produced for one Context item."""

    files: tuple[str, ...]
    index: ContextIndexEntry


class ContextAdapter(Protocol):
    """The complete source-specific side of Context extraction and rendering."""

    source_kind: str
    object_kinds: frozenset[str]
    attribution_mode: AttributionMode
    index_section: str
    empty_index_message: str

    def project(
        self, snapshot: PublishedSnapshot, start: datetime, end: datetime
    ) -> object | None:
        """Project and source-filter one selected Snapshot."""

    def include(self, projection: object) -> bool:
        """Apply source-specific item inclusion semantics."""

    def prepare(
        self,
        items: tuple[ContextItem, ...],
        runs: tuple[PublishedRun, ...],
        archive_root: str | Path | None,
    ) -> tuple[ContextItem, ...]:
        """Enrich items and assign deterministic source-specific paths."""

    def render(
        self, item: ContextItem, result: ContextExtractionResult, output: Path
    ) -> RenderedContextItem:
        """Render one source item and return its shared-index metadata."""

    def index_header(self, items: tuple[ContextItem, ...]) -> tuple[str, ...]:
        """Return optional source-specific lines before the item entries."""

    def index_metadata(
        self, item: ContextItem, result: ContextExtractionResult
    ) -> ContextIndexEntry:
        """Return source-specific label and grouping values for the index."""


def rendered_context_item(
    adapter: ContextAdapter,
    item: ContextItem,
    result: ContextExtractionResult,
    files: Iterable[str],
) -> RenderedContextItem:
    """Combine source-rendered files with the adapter's index metadata."""
    return RenderedContextItem(tuple(files), adapter.index_metadata(item, result))


def render_source_item(
    adapter: ContextAdapter,
    item: ContextItem,
    result: ContextExtractionResult,
    output: Path,
    renderer: Callable[[ContextItem, ContextExtractionResult, Path], Iterable[str]],
) -> RenderedContextItem:
    """Run a source renderer and attach its shared-index metadata."""
    return rendered_context_item(adapter, item, result, renderer(item, result, output))


def number_context_items(
    indexed_items: tuple[tuple[int, ContextItem], ...],
    path_for_number: Callable[[int], str],
    sort_key: Callable[[ContextItem], tuple[datetime, str, str]],
) -> tuple[tuple[int, ContextItem], ...]:
    """Assign two-digit paths by a source-specific deterministic sort key."""
    numbered: dict[int, ContextItem] = {}
    for number, (index, item) in enumerate(
        sorted(indexed_items, key=lambda pair: sort_key(pair[1])), start=1
    ):
        numbered[index] = replace(item, path=path_for_number(number))
    return tuple((index, numbered[index]) for index, _item in indexed_items)


def safe_path_component(value: str) -> str:
    """Keep adapter-generated Context paths inside their source namespace."""
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return component if component not in {"", ".", ".."} else "unknown"
