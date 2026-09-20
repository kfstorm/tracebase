"""Validation, reconciliation, and rendering for host-side state metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .context_adapter import MutableStateObservation
from .context_inventory import ContextInventory

_OBSERVATION_FIELDS = {
    "entity_key",
    "entity_label",
    "observed_at",
    "observed_after_request_end",
    "fields",
}
_FIELD_PAIR_LENGTH = 2


class MutableStateError(ValueError):
    """Raised when host-side mutable-state metadata is invalid."""


@dataclass(frozen=True, slots=True)
class MutableStateItem:
    """Validated observations for one Context item root."""

    root: str
    observations: tuple[MutableStateObservation, ...]


def _observed_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MutableStateError("mutable-state observation timestamp is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise MutableStateError(
            "mutable-state observation timestamp is invalid"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MutableStateError("mutable-state observation timestamp is invalid")
    return parsed


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise MutableStateError("mutable-state metadata must be a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        raise MutableStateError("mutable-state metadata is not valid JSON") from None


def _observation(raw: Any) -> MutableStateObservation:
    if not isinstance(raw, dict) or set(raw) != _OBSERVATION_FIELDS:
        raise MutableStateError("mutable-state observation has invalid fields")
    entity_key = raw["entity_key"]
    entity_label = raw["entity_label"]
    if not isinstance(entity_key, str) or not entity_key:
        raise MutableStateError("mutable-state entity_key must be non-empty")
    if not isinstance(entity_label, str) or not entity_label:
        raise MutableStateError("mutable-state entity_label must be non-empty")
    observed_at = raw["observed_at"]
    _observed_at(observed_at)
    observed_after_request_end = raw["observed_after_request_end"]
    if not isinstance(observed_after_request_end, bool):
        raise MutableStateError(
            "mutable-state observed_after_request_end must be a boolean"
        )
    raw_fields = raw["fields"]
    if not isinstance(raw_fields, list) or not raw_fields:
        raise MutableStateError("mutable-state fields must be a non-empty list")
    fields: list[tuple[str, str]] = []
    names: set[str] = set()
    for pair in raw_fields:
        if (
            not isinstance(pair, list)
            or len(pair) != _FIELD_PAIR_LENGTH
            or not all(isinstance(value, str) for value in pair)
        ):
            raise MutableStateError("mutable-state fields must contain string pairs")
        name, value = pair
        if not name or name in names:
            raise MutableStateError("mutable-state field names must be unique")
        names.add(name)
        fields.append((name, value))
    return MutableStateObservation(
        entity_key,
        entity_label,
        observed_at,
        tuple(fields),
        observed_after_request_end,
    )


def load_mutable_state(
    context_dir: Path, inventory: ContextInventory
) -> tuple[MutableStateItem, ...]:
    """Load and validate host-only state metadata against Context inventory."""
    raw = _load_json(context_dir / "mutable-state.json")
    if not isinstance(raw, dict) or set(raw) != {"items"}:
        raise MutableStateError("mutable-state metadata must contain only items")
    raw_items = raw["items"]
    if not isinstance(raw_items, list) or len(raw_items) != len(inventory.items):
        raise MutableStateError("mutable-state item inventory does not match Context")

    items: list[MutableStateItem] = []
    roots: set[str] = set()
    for expected, raw_item in zip(inventory.items, raw_items, strict=True):
        if not isinstance(raw_item, dict) or set(raw_item) != {"root", "observations"}:
            raise MutableStateError("mutable-state item has invalid fields")
        root = raw_item["root"]
        if not isinstance(root, str) or root != expected.root:
            raise MutableStateError("mutable-state item roots do not match Context")
        if root in roots:
            raise MutableStateError(f"mutable-state root {root!r} is duplicated")
        roots.add(root)
        raw_observations = raw_item["observations"]
        if not isinstance(raw_observations, list):
            raise MutableStateError("mutable-state observations must be a list")
        items.append(
            MutableStateItem(
                root, tuple(_observation(value) for value in raw_observations)
            )
        )
    return tuple(items)


def observation_json(observation: MutableStateObservation) -> dict[str, Any]:
    """Serialize one validated observation using the host metadata schema."""
    return {
        "entity_key": observation.entity_key,
        "entity_label": observation.entity_label,
        "observed_at": observation.observed_at,
        "observed_after_request_end": observation.observed_after_request_end,
        "fields": [list(field) for field in observation.fields],
    }


def reconcile_mutable_state(
    items: tuple[MutableStateItem, ...],
) -> tuple[MutableStateObservation, ...]:
    """Select the latest complete observation for each exact entity key."""
    selected: dict[str, MutableStateObservation] = {}
    parsed: dict[str, datetime] = {}
    for item in items:
        for observation in item.observations:
            timestamp = _observed_at(observation.observed_at)
            previous = selected.get(observation.entity_key)
            if previous is None:
                selected[observation.entity_key] = observation
                parsed[observation.entity_key] = timestamp
                continue
            previous_timestamp = parsed[observation.entity_key]
            if timestamp == previous_timestamp:
                if observation != previous:
                    raise MutableStateError(
                        "conflicting mutable-state observations share a timestamp"
                    )
                continue
            if timestamp > previous_timestamp:
                selected[observation.entity_key] = observation
                parsed[observation.entity_key] = timestamp
    return tuple(selected[key] for key in sorted(selected))


def render_mutable_state(
    observations: tuple[MutableStateObservation, ...],
) -> str:
    """Render a concise deterministic ledger for the root summarizer."""
    lines = [
        "# Host-reconciled mutable external state",
        "",
        "This file is generated deterministically from selected Context observations.",
        "For entities listed here, these are the reconciled selected-observation "
        "fields to use when the final Summary states final/current mutable external "
        "state. Worker-report state mentions are historical/contextual and must not "
        "override a matching entry here.",
        "",
        "This is not a globally synchronized view beyond the selected observations.",
        "Preserve any caveat stated for an entry. Do not infer an entity match when "
        "a stable identifier does not establish one.",
        "",
    ]
    if not observations:
        lines.append("No host-reconciled mutable external state is available.")
        return "\n".join(lines) + "\n"
    for index, observation in enumerate(observations):
        if index:
            lines.append("")
        lines.extend(
            [
                f"## {observation.entity_label}",
                "",
                f"- Entity: {observation.entity_key}",
                f"- Observed at: {observation.observed_at}",
            ]
        )
        for name, value in observation.fields:
            label = name.replace("_", " ").capitalize()
            lines.append(f"- {label}: {value}")
        if observation.observed_after_request_end:
            lines.append(
                "- Caveat: selected mutable fields may include changes observed "
                "after the requested interval boundary."
            )
    return "\n".join(lines) + "\n"
