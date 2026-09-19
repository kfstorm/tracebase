import json
import shutil
from pathlib import Path

import pytest

from tracebase.context_inventory import ContextInventoryError, load_context_inventory
from tracebase.summary_planner import ShardPolicy, plan_shards


def _context(
    tmp_path: Path,
    items: list[tuple[str, str | None, str, int]],
) -> Path:
    context = tmp_path / "context"
    context.mkdir()
    inventory = []
    for root, group, mode, payload_size in items:
        item_root = context.joinpath(*root.split("/"))
        item_root.mkdir(parents=True)
        (item_root / "overview.md").write_text(root, encoding="utf-8")
        if payload_size:
            (item_root / "activity.md").write_bytes(b"x" * payload_size)
        inventory.append(
            {
                "root": root,
                "attribution_mode": mode,
                "group": group,
                "files": ["overview.md"],
            }
        )
    (context / "index.json").write_text(
        json.dumps({"items": inventory}) + "\n", encoding="utf-8"
    )
    return context


def test_inventory_preserves_order_and_generic_metadata(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("synthetic/source/item-a", "opaque-a", "personal", 0),
            ("future/source/item-b", None, "actor_scoped", 0),
        ],
    )

    inventory = load_context_inventory(context)

    assert [item.root for item in inventory.items] == [
        "synthetic/source/item-a",
        "future/source/item-b",
    ]
    assert inventory.items[0].attribution_mode.value == "personal"
    assert inventory.items[0].group == "opaque-a"
    assert inventory.items[1].group is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["items"].append(value["items"][0].copy()),
        lambda value: value["items"][0].__setitem__("root", "missing/item"),
        lambda value: value["items"][0].__setitem__("attribution_mode", "unknown"),
    ],
)
def test_inventory_rejects_duplicate_missing_or_malformed_items(
    tmp_path: Path, mutate
) -> None:
    context = _context(tmp_path, [("one", None, "personal", 0)])
    inventory_path = context / "index.json"
    value = json.loads(inventory_path.read_text(encoding="utf-8"))
    mutate(value)
    inventory_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ContextInventoryError):
        load_context_inventory(context)


def test_inventory_missing_item_directory_is_not_measured_as_zero(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, [("one", None, "personal", 0)])
    shutil.rmtree(context / "one")

    with pytest.raises(ContextInventoryError, match="directory"):
        load_context_inventory(context)


def test_normal_natural_group_remains_intact(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [("one", "group", "personal", 9000), ("two", "group", "personal", 9000)],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [("one", "two")]


def test_oversized_multi_item_group_splits_in_stable_order(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [("one", "group", "personal", 60), ("two", "group", "personal", 60)],
    )

    plan = plan_shards(
        context, ShardPolicy(max_bytes=100, max_items=8, tiny_group_bytes=0)
    )

    assert [shard.items for shard in plan] == [("one",), ("two",)]


def test_oversized_item_remains_alone(tmp_path: Path) -> None:
    context = _context(tmp_path, [("one", "group", "personal", 200)])

    plan = plan_shards(
        context, ShardPolicy(max_bytes=100, max_items=8, tiny_group_bytes=0)
    )

    assert plan[0].items == ("one",)
    assert plan[0].readable_bytes > 100


def test_tiny_groups_pack_across_source_boundaries(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("github/item", "github-group", "actor_scoped", 1),
            ("future/item", "future-group", "personal", 1),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [("github/item", "future/item")]


def test_non_tiny_natural_groups_are_not_cross_packed(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("one", "one", "personal", 17000),
            ("two", "two", "personal", 17000),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [("one",), ("two",)]


def test_group_aggregate_controls_tiny_eligibility(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("group/one", "natural", "personal", 6000),
            ("group/two", "natural", "personal", 6000),
            ("group/three", "natural", "personal", 6000),
            ("other", "other", "personal", 1),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        ("group/one", "group/two", "group/three"),
        ("other",),
    ]


def test_item_limit_is_enforced_without_splitting_a_valid_item(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path,
        [(f"item-{index}", "group", "personal", 1) for index in range(3)],
    )

    plan = plan_shards(
        context, ShardPolicy(max_bytes=100_000, max_items=2, tiny_group_bytes=0)
    )

    assert [shard.items for shard in plan] == [("item-0", "item-1"), ("item-2",)]


def test_stable_input_and_opaque_roots_produce_stable_plan(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("source-a/opaque-1", "same", "personal", 9000),
            ("source-b/opaque-2", "same", "personal", 9000),
        ],
    )

    first = plan_shards(context)
    second = plan_shards(context)

    assert first == second
