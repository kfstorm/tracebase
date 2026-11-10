"""Shared contracts for source collectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .archive import CollectionRange


@dataclass(frozen=True, slots=True)
class DiscoverySince:
    """Discover mutable objects that may contain activity in a Collection Range.

    A later update must not hide earlier activity during delayed collection.
    Deliberately expose no collection end to source discovery adapters; Context
    applies the requested interval to individual records after hydration.
    """

    start: datetime

    @classmethod
    def for_collection(cls, collection_range: CollectionRange) -> DiscoverySince:
        return cls(collection_range.start)

    def includes(self, updated: datetime) -> bool:
        return updated >= self.start


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """The collected coverage ready for publication."""

    coverage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CollectionContext:
    """Source-resolved identity and options used to create a Collection Run."""

    source_kind: str
    scope_id: str
    collector_version: str
    effective_options: dict[str, Any]
