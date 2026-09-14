"""Source attribution semantics used by Context and Summary consumers."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class AttributionError(ValueError):
    """Raised when a source has no explicitly declared attribution semantics."""


class AttributionMode(StrEnum):
    """How source evidence is projected into the user's work."""

    PERSONAL = "personal"
    ACTOR_SCOPED = "actor_scoped"


SOURCE_ATTRIBUTION_MODES: Final[Mapping[str, AttributionMode]] = MappingProxyType(
    {
        "opencode": AttributionMode.PERSONAL,
        "chatgpt": AttributionMode.PERSONAL,
        "github": AttributionMode.ACTOR_SCOPED,
    }
)


def source_attribution_mode(source_kind: str) -> AttributionMode:
    """Return the declared mode, refusing unknown sources instead of guessing."""
    try:
        return SOURCE_ATTRIBUTION_MODES[source_kind]
    except KeyError:
        raise AttributionError(
            f"source kind {source_kind!r} has no attribution semantics"
        ) from None
