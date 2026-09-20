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
class ContextOrdering:
    """Source-provided values needed for deterministic item ordering."""

    group: str | None
    sort_key: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderedContextItem:
    """The source files and ordering values produced for one Context item."""

    files: tuple[str, ...]
    ordering: ContextOrdering


class ContextAdapter(Protocol):
    """The complete source-specific side of Context extraction and rendering."""

    source_kind: str
    object_kinds: frozenset[str]
    attribution_mode: AttributionMode

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
        """Render one source item and return its ordering metadata."""

    def ordering_metadata(
        self, item: ContextItem, result: ContextExtractionResult
    ) -> ContextOrdering:
        """Return source-specific values for deterministic item ordering."""


def rendered_context_item(
    adapter: ContextAdapter,
    item: ContextItem,
    result: ContextExtractionResult,
    files: Iterable[str],
) -> RenderedContextItem:
    """Combine source-rendered files with the adapter's ordering metadata."""
    return RenderedContextItem(tuple(files), adapter.ordering_metadata(item, result))


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
    """Assign numbered paths by a source-specific deterministic sort key."""
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
