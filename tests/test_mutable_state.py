import json
from datetime import datetime
from pathlib import Path

import pytest

from tracebase.context_adapter import MutableStateObservation
from tracebase.context_inventory import load_context_inventory
from tracebase.mutable_state import (
    MutableStateError,
    MutableStateItem,
    load_mutable_state,
    reconcile_mutable_state,
    render_mutable_state,
)


def _context(tmp_path: Path) -> Path:
    context = tmp_path / "context"
    context.mkdir()
    items = []
    for root in ("first/item", "second/item"):
        item = context.joinpath(*root.split("/"))
        item.mkdir(parents=True)
        (item / "overview.md").write_text("synthetic evidence\n", encoding="utf-8")
        items.append({"root": root, "files": ["overview.md"]})
    (context / "index.json").write_text(
        json.dumps(
            {
                "requested_interval": {
                    "from": "2026-01-01T00:00:00+00:00",
                    "to": "2026-01-02T00:00:00+00:00",
                },
                "items": items,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return context


def _observation(
    entity_key: str = "https://example.com/entity/1",
    *,
    observed_at: str = "2026-01-01T01:00:00+00:00",
    state: str = "open",
    after: bool = False,
) -> MutableStateObservation:
    return MutableStateObservation(
        entity_key,
        "synthetic entity",
        observed_at,
        (("state", state),),
        after,
    )


def _write_state(
    context: Path,
    items: list[dict[str, object]],
) -> None:
    (context / "mutable-state.json").write_text(
        json.dumps({"items": items}) + "\n", encoding="utf-8"
    )


def test_mutable_state_validation_requires_inventory_order(tmp_path: Path) -> None:
    context = _context(tmp_path)
    inventory = load_context_inventory(context)
    valid = [
        {"root": "first/item", "observations": []},
        {"root": "second/item", "observations": []},
    ]

    for _name, items in (
        ("missing", valid[:1]),
        ("extra", [*valid, {"root": "extra", "observations": []}]),
        (
            "duplicate",
            [valid[0], {"root": "first/item", "observations": []}],
        ),
        ("reordered", list(reversed(valid))),
    ):
        _write_state(context, items)
        with pytest.raises(MutableStateError):
            load_mutable_state(context, inventory)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda observation: observation.pop("observed_at"),
        lambda observation: observation.update(observed_at="not-a-timestamp"),
        lambda observation: observation.update(fields=[["state"]]),
        lambda observation: observation.update(
            fields=[["state", "open"], ["state", "closed"]]
        ),
    ],
)
def test_mutable_state_validation_rejects_malformed_observations(
    tmp_path: Path, mutate: object
) -> None:
    context = _context(tmp_path)
    inventory = load_context_inventory(context)
    observation: dict[str, object] = {
        "entity_key": "https://example.com/entity/1",
        "entity_label": "synthetic entity",
        "observed_at": "2026-01-01T01:00:00+00:00",
        "observed_after_request_end": False,
        "fields": [["state", "open"]],
    }
    assert callable(mutate)
    mutate(observation)  # type: ignore[operator]
    _write_state(
        context,
        [
            {"root": "first/item", "observations": [observation]},
            {"root": "second/item", "observations": []},
        ],
    )
    with pytest.raises(MutableStateError):
        load_mutable_state(context, inventory)


def test_reconcile_preserves_single_observation_and_caveat() -> None:
    observation = _observation(after=True)
    assert reconcile_mutable_state((MutableStateItem("first", (observation,)),)) == (
        observation,
    )
    assert "Caveat:" in render_mutable_state((observation,))


def test_reconcile_uses_latest_exact_entity_observation() -> None:
    earlier = _observation(observed_at="2026-01-01T01:00:00+00:00")
    later = _observation(observed_at="2026-01-01T02:00:00+00:00", state="merged")
    other = _observation("https://example.com/entity/2")
    assert reconcile_mutable_state(
        (
            MutableStateItem("path-a", (later, other)),
            MutableStateItem("path-b", (earlier,)),
        )
    ) == (later, other)


def test_reconcile_deduplicates_identical_same_time_observations() -> None:
    observation = _observation()
    assert reconcile_mutable_state(
        (
            MutableStateItem("one", (observation,)),
            MutableStateItem("two", (observation,)),
        )
    ) == (observation,)


def test_reconcile_rejects_conflicting_same_time_observations() -> None:
    first = _observation()
    second = _observation(state="closed")
    with pytest.raises(MutableStateError, match="conflicting"):
        reconcile_mutable_state((MutableStateItem("one", (first, second)),))


def test_reconcile_does_not_match_by_label_or_context_path() -> None:
    first = _observation("https://example.com/entity/1")
    second = MutableStateObservation(
        "https://example.com/entity/2",
        first.entity_label,
        first.observed_at,
        first.fields,
        first.observed_after_request_end,
    )
    selected = reconcile_mutable_state(
        (MutableStateItem("same/title/path", (first, second)),)
    )
    assert [observation.entity_key for observation in selected] == [
        "https://example.com/entity/1",
        "https://example.com/entity/2",
    ]


def test_render_empty_mutable_state_is_explicit() -> None:
    rendered = render_mutable_state(())
    assert "No host-reconciled mutable external state is available." in rendered
    assert "Caveat:" not in rendered


def test_observed_at_is_offset_aware_for_reconciled_values() -> None:
    parsed = datetime.fromisoformat(_observation().observed_at)
    assert parsed.utcoffset() is not None
