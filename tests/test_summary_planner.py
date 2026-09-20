import json
import shutil
from pathlib import Path

import pytest

from tracebase.context_inventory import ContextInventoryError, load_context_inventory
from tracebase.summary_planner import (
    SHARD_POLICY,
    SUMMARY_SHARD_MAX_BYTES,
    PlannedShard,
    ShardPolicy,
    plan_shards,
    shard_task_text,
    write_initial_plan,
)
from tracebase.summary_shard_validator import inspect_shard_plan


def _context(
    tmp_path: Path,
    items: list[tuple[str, int]],
) -> Path:
    context = tmp_path / "context"
    context.mkdir()
    inventory = []
    for root, payload_size in items:
        item_root = context.joinpath(*root.split("/"))
        item_root.mkdir(parents=True)
        (item_root / "overview.md").write_text(root, encoding="utf-8")
        if payload_size:
            (item_root / "activity.md").write_bytes(b"x" * payload_size)
        files = ["overview.md"]
        if payload_size:
            files.append("activity.md")
        inventory.append(
            {
                "root": root,
                "files": files,
            }
        )
    (context / "index.json").write_text(
        json.dumps(
            {
                "requested_interval": {
                    "from": "2026-01-01T00:00:00+00:00",
                    "to": "2026-01-02T00:00:00+00:00",
                },
                "items": inventory,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return context


def test_policy_has_only_the_authoritative_limits() -> None:
    policy = ShardPolicy()

    assert SUMMARY_SHARD_MAX_BYTES == 256 * 1024
    assert policy.max_bytes == SUMMARY_SHARD_MAX_BYTES
    assert SHARD_POLICY.max_bytes == SUMMARY_SHARD_MAX_BYTES
    assert policy.max_items == 8
    assert not hasattr(policy, "tiny_group_bytes")


def test_inventory_preserves_order_without_extra_metadata(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("synthetic/source/item-a", 0),
            ("future/source/item-b", 0),
        ],
    )

    inventory = load_context_inventory(context)

    assert [item.root for item in inventory.items] == [
        "synthetic/source/item-a",
        "future/source/item-b",
    ]
    assert not hasattr(inventory.items[0], "group")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["items"].append(value["items"][0].copy()),
        lambda value: value["items"][0].__setitem__("root", "missing/item"),
        lambda value: value["items"][0].__setitem__("extra", "forbidden"),
        lambda value: value["items"][0].__setitem__("group", "forbidden"),
    ],
)
def test_inventory_rejects_duplicate_missing_or_malformed_items(
    tmp_path: Path, mutate
) -> None:
    context = _context(tmp_path, [("one", 0)])
    inventory_path = context / "index.json"
    value = json.loads(inventory_path.read_text(encoding="utf-8"))
    mutate(value)
    inventory_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ContextInventoryError):
        load_context_inventory(context)


def test_inventory_rejects_overlapping_item_roots(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [("project", 0), ("project/item", 0)],
    )

    with pytest.raises(ContextInventoryError, match="overlaps"):
        load_context_inventory(context)


def test_inventory_missing_item_directory_is_not_measured_as_zero(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, [("one", 0)])
    shutil.rmtree(context / "one")

    with pytest.raises(ContextInventoryError, match="directory"):
        load_context_inventory(context)


def test_inventory_rejects_legacy_index_markdown(tmp_path: Path) -> None:
    context = _context(tmp_path, [("one", 0)])
    (context / "index.md").write_text("legacy", encoding="utf-8")

    with pytest.raises(ContextInventoryError, match=r"index\.md"):
        load_context_inventory(context)


def test_intact_small_subtrees_pack_across_sources(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("github/item", 80_000),
            ("opencode/item", 80_000),
            ("chatgpt/item", 80_000),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        ("github/item", "opencode/item", "chatgpt/item")
    ]
    assert plan[0].readable_bytes <= SHARD_POLICY.max_bytes


def test_overflowing_subtree_is_isolated_from_intact_siblings(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("github/repo/item-a", 280_000),
            ("opencode/item", 100_000),
            ("chatgpt/item", 100_000),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        ("github/repo/item-a",),
        ("opencode/item", "chatgpt/item"),
    ]


def test_intact_siblings_may_pack_around_isolated_overflow_units(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path,
        [
            ("a", 20_000),
            ("b/item-1", 140_000),
            ("b/item-2", 140_000),
            ("c", 20_000),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        ("a", "c"),
        ("b/item-1",),
        ("b/item-2",),
    ]


def test_packed_items_follow_interleaved_inventory_order(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path,
        [
            ("a/item-1", 10_000),
            ("b/item-1", 140_000),
            ("c/item-1", 10_000),
            ("a/item-2", 10_000),
            ("b/item-2", 140_000),
            ("c/item-2", 10_000),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        ("a/item-1", "c/item-1", "a/item-2", "c/item-2"),
        ("b/item-1",),
        ("b/item-2",),
    ]


def test_overflowing_project_subtree_does_not_use_sibling_capacity(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path,
        [
            ("github/project/item-a", 140_000),
            ("github/project/item-b", 140_000),
            ("other/item", 20_000),
        ],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        ("github/project/item-a",),
        ("github/project/item-b",),
        ("other/item",),
    ]


def test_item_count_triggers_recursive_splitting(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [(f"project/item-{index}", 1) for index in range(9)],
    )

    plan = plan_shards(context)

    assert [shard.items for shard in plan] == [
        tuple(f"project/item-{index}" for index in range(8)),
        ("project/item-8",),
    ]


def test_oversized_item_remains_alone(tmp_path: Path) -> None:
    context = _context(tmp_path, [("one", 200)])

    plan = plan_shards(context, ShardPolicy(max_bytes=100, max_items=8))

    assert plan[0].items == ("one",)
    assert plan[0].readable_bytes > 100


def test_item_limit_is_enforced_without_splitting_a_valid_item(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path,
        [(f"item-{index}", 1) for index in range(3)],
    )

    plan = plan_shards(context, ShardPolicy(max_bytes=100_000, max_items=2))

    assert [shard.items for shard in plan] == [
        ("item-0", "item-1"),
        ("item-2",),
    ]


def test_stable_input_produces_stable_plan(tmp_path: Path) -> None:
    context = _context(
        tmp_path,
        [
            ("source-z/opaque-1", 9000),
            ("source-a/opaque-2", 9000),
        ],
    )

    first = plan_shards(context)
    second = plan_shards(context)

    assert first == second
    assert first[0].items == ("source-z/opaque-1", "source-a/opaque-2")


def test_initial_plan_materializes_self_contained_worker_packages(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path,
        [("one", 0), ("two", 0)],
    )
    work = tmp_path / "work"
    plan = plan_shards(context)

    write_initial_plan(work, context, plan)

    shard_dir = work / "shards" / "shard-01"
    assert {path.name for path in shard_dir.iterdir()} == {"TASK.md", "STATUS.json"}
    assert not (shard_dir / "REPORT.md").exists()
    assert json.loads((shard_dir / "STATUS.json").read_text()) == {
        "status": "pending",
        "retry_count": 0,
    }
    task = (shard_dir / "TASK.md").read_text()
    assert "- /context/one/overview.md" in task
    assert "- /context/two/overview.md" in task
    assert "Do not read or modify `/work/TASK.md`" in task
    assert "Write exactly one report to:" in task
    notes = (work / "NOTES.md").read_text()
    assert "Requested interval: [2026-01-01T00:00:00+00:00, " in notes
    assert notes.count("- shard-") == len(plan)
    assert "- shard-01" in notes
    assert "SHARD_STATUS" not in notes


def test_shard_task_assignments_match_every_manifest_file(tmp_path: Path) -> None:
    context = _context(tmp_path, [("source/item", 1)])
    inventory_path = context / "index.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["items"][0]["files"].append("nested/evidence.md")
    item = context / "source/item/nested"
    item.mkdir()
    (item / "evidence.md").write_text("nested", encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory) + "\n", encoding="utf-8")

    plan = PlannedShard("shard-01", ("source/item",), 0)

    rendered = shard_task_text(plan, load_context_inventory(context))
    assigned = (
        rendered.split("## Assigned evidence\n\n", 1)[1]
        .split("\n\n## Worker contract", 1)[0]
        .splitlines()
    )

    assert assigned == [
        "- /context/source/item/overview.md",
        "- /context/source/item/activity.md",
        "- /context/source/item/nested/evidence.md",
    ]


def test_shard_task_rejects_unknown_membership(tmp_path: Path) -> None:
    context = _context(tmp_path, [("source/item", 0)])

    with pytest.raises(ValueError, match="unknown Context item"):
        write_initial_plan(
            tmp_path / "work",
            context,
            (PlannedShard("shard-01", ("missing/item",), 0),),
        )


def test_validator_ignores_filesystem_order_for_large_plan(tmp_path: Path) -> None:
    context = _context(tmp_path, [])
    work = tmp_path / "work"
    plan = tuple(PlannedShard(f"shard-{index:02d}", (), 0) for index in range(1, 101))

    write_initial_plan(work, context, plan)

    assert inspect_shard_plan(work, context, expected_plan=plan).errors == ()
