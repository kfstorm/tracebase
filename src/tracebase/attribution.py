"""Shared attribution policies used by Context consumers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AttributionPolicy:
    """Source-declared semantics rendered into consumer-facing Context."""

    context_guidance: str


CONVERSATIONAL_ATTRIBUTION_POLICY = AttributionPolicy(
    "Work-related conversational activity, including delegated cognitive work, "
    "is user work; non-work activity is context-only."
)

ACTOR_SCOPED_ATTRIBUTION_POLICY = AttributionPolicy(
    "Only records explicitly marked [User work] are attributable user work; "
    "[Context only] remains context-only."
)
