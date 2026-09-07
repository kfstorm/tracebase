"""Shared contracts for source collectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """The collected coverage and number of snapshots ready for publication."""

    coverage: dict[str, Any]
    snapshot_count: int
