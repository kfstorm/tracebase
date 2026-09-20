"""Shared interpretation policies used by Context consumers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AttributionPolicy:
    """Source-declared semantics rendered into consumer-facing Context."""

    context_guidance: str


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    """Source-declared evidence and mutable-state semantics."""

    evidence_guidance: str


CONVERSATIONAL_ATTRIBUTION_POLICY = AttributionPolicy(
    "Work-related conversational activity, including delegated cognitive work, "
    "is user work; non-work activity is context-only."
)

CONVERSATIONAL_EVIDENCE_POLICY = EvidencePolicy(
    "Only retained text turns are present. Activity records are eligible to "
    "establish work in the requested interval; background records are earlier "
    "supporting context only, and later turns are not included. This item can "
    "establish reported work, analysis, decisions, implementation, commands, "
    "tests, or validation, but assistant output alone does not prove an external "
    "side effect. Point-in-time observations about mutable external state remain "
    "observations and cannot by themselves establish final or current state."
)

CHATGPT_EVIDENCE_POLICY = EvidencePolicy(
    "Only retained text turns are present. Activity records are eligible to "
    "establish work in the requested interval; background records are earlier "
    "supporting context only, and later turns are not included. This item is the "
    "observed conversation stream; historical branches, regenerations, and edits "
    "are not reconstructed. It can establish reported work, analysis, decisions, "
    "implementation, commands, tests, or validation, but assistant output alone "
    "does not prove an external side effect. Point-in-time observations about "
    "mutable external state remain observations and cannot by themselves establish "
    "final or current state."
)

ACTOR_SCOPED_ATTRIBUTION_POLICY = AttributionPolicy(
    "Only records explicitly marked [User work] are attributable user work; "
    "[Context only] remains context-only. Record-level annotations are "
    "authoritative, and unmarked actor names do not establish attribution."
)

PROVIDER_EVIDENCE_POLICY = EvidencePolicy(
    "This item contains selected provider observations of mutable external state. "
    "Those observations may participate in final-state reconciliation, subject to "
    "any observation-window caveat stated in this item, but they are not a globally "
    "synchronized or authoritative view beyond the selected observation. Record-level "
    "attribution annotations remain separate from these state semantics."
)
