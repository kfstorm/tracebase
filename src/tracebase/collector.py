"""Shared contracts for source collectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


@dataclass(frozen=True, slots=True)
class GitHubContext(CollectionContext):
    """Resolved GitHub actor context used by the GitHub collector."""

    actor_login: str
