"""Deterministic host-side planning for Summary execution shards."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .context_inventory import (
    ContextInventory,
    ContextInventoryError,
    ContextInventoryItem,
    item_readable_sizes,
    load_context_inventory,
)


@dataclass(frozen=True, slots=True)
class ShardPolicy:
    """Authoritative generic execution limits for every Context source."""

    max_bytes: int = 256 * 1024
    max_items: int = 8
    tiny_group_bytes: int = 16 * 1024


SHARD_POLICY = ShardPolicy()


@dataclass(frozen=True, slots=True)
class PlannedShard:
    """A host-generated immutable shard assignment and its measured size."""

    id: str
    items: tuple[str, ...]
    readable_bytes: int


@dataclass(frozen=True, slots=True)
class _PlanningUnit:
    items: tuple[str, ...]
    readable_bytes: int
    first_index: int


def _split_group(
    group: tuple[ContextInventoryItem, ...],
    sizes: dict[str, int],
    policy: ShardPolicy,
) -> tuple[_PlanningUnit, ...]:
    """Split only groups that violate a normal shard limit."""
    total = sum(sizes[item.root] for item in group)
    if len(group) <= policy.max_items and total <= policy.max_bytes:
        return (
            _PlanningUnit(
                tuple(item.root for item in group),
                total,
                0,
            ),
        )

    units: list[_PlanningUnit] = []
    current: list[ContextInventoryItem] = []
    current_bytes = 0
    for item in group:
        item_bytes = sizes[item.root]
        exceeds_current = current and (
            len(current) >= policy.max_items
            or current_bytes + item_bytes > policy.max_bytes
        )
        if exceeds_current:
            units.append(
                _PlanningUnit(
                    tuple(part.root for part in current),
                    current_bytes,
                    0,
                )
            )
            current = []
            current_bytes = 0
        if item_bytes > policy.max_bytes:
            if current:
                units.append(
                    _PlanningUnit(
                        tuple(part.root for part in current),
                        current_bytes,
                        0,
                    )
                )
                current = []
                current_bytes = 0
            units.append(_PlanningUnit((item.root,), item_bytes, 0))
            continue
        current.append(item)
        current_bytes += item_bytes
    if current:
        units.append(
            _PlanningUnit(
                tuple(part.root for part in current),
                current_bytes,
                0,
            )
        )
    return tuple(units)


def _natural_groups(
    inventory: ContextInventory,
) -> tuple[tuple[ContextInventoryItem, ...], ...]:
    groups: dict[str, list[ContextInventoryItem]] = {}
    for index, item in enumerate(inventory.items):
        key = item.group if item.group is not None else f"\x00{index}"
        groups.setdefault(key, []).append(item)
    return tuple(tuple(group) for group in groups.values())


def _with_first_indices(
    units: Iterable[_PlanningUnit], inventory: ContextInventory
) -> tuple[_PlanningUnit, ...]:
    positions = {item.root: index for index, item in enumerate(inventory.items)}
    return tuple(
        _PlanningUnit(
            unit.items,
            unit.readable_bytes,
            min(positions[item] for item in unit.items),
        )
        for unit in units
    )


def _pack_tiny_groups(
    groups: tuple[_PlanningUnit, ...],
    policy: ShardPolicy,
) -> tuple[_PlanningUnit, ...]:
    packed: list[_PlanningUnit] = []
    current_items: list[str] = []
    current_bytes = 0
    current_first_index = 0
    for group in groups:
        if current_items and (
            len(current_items) + len(group.items) > policy.max_items
            or current_bytes + group.readable_bytes > policy.max_bytes
        ):
            packed.append(
                _PlanningUnit(tuple(current_items), current_bytes, current_first_index)
            )
            current_items = []
            current_bytes = 0
        if not current_items:
            current_first_index = group.first_index
        current_items.extend(group.items)
        current_bytes += group.readable_bytes
    if current_items:
        packed.append(
            _PlanningUnit(tuple(current_items), current_bytes, current_first_index)
        )
    return tuple(packed)


def plan_shards(
    context_dir: Path, policy: ShardPolicy = SHARD_POLICY
) -> tuple[PlannedShard, ...]:
    """Create a stable generic shard partition without reading evidence content."""
    try:
        inventory = load_context_inventory(context_dir)
        sizes = item_readable_sizes(context_dir, inventory)
    except ContextInventoryError as error:
        raise ValueError(str(error)) from None

    fixed: list[_PlanningUnit] = []
    tiny: list[_PlanningUnit] = []
    for group in _natural_groups(inventory):
        split = _with_first_indices(_split_group(group, sizes, policy), inventory)
        if len(split) != 1:
            fixed.extend(split)
            continue
        unit = split[0]
        if unit.readable_bytes <= policy.tiny_group_bytes:
            tiny.append(unit)
        else:
            fixed.append(unit)

    units = [*fixed, *_pack_tiny_groups(tuple(tiny), policy)]
    units.sort(key=lambda unit: unit.first_index)
    return tuple(
        PlannedShard(f"shard-{index:02d}", unit.items, unit.readable_bytes)
        for index, unit in enumerate(units, start=1)
    )


def initial_status(
    plan: tuple[PlannedShard, ...],
) -> dict[str, list[dict[str, object]]]:
    """Serialize a host plan using the mutable worker-status schema."""
    return {
        "shards": [
            {
                "id": shard.id,
                "items": list(shard.items),
                "status": "pending",
                "retry_count": 0,
                "report": f"/work/shards/{shard.id}.md",
            }
            for shard in plan
        ]
    }


def write_initial_plan(work_dir: Path, plan: tuple[PlannedShard, ...]) -> None:
    """Write durable host-owned notes and the complete pending inventory."""
    work_dir.joinpath("shards").mkdir(parents=True, exist_ok=True)
    status = json.dumps(initial_status(plan), ensure_ascii=False, separators=(",", ":"))
    notes = (
        "# Summary shard plan\n\n"
        "Tracebase generated and validated this execution partition before the "
        "root model started. Shard membership is an execution partition only; "
        "it does not establish a semantic or workstream relationship.\n\n"
        "<!-- SHARD_STATUS_BEGIN -->\n"
        f"{status}\n"
        "<!-- SHARD_STATUS_END -->\n"
    )
    work_dir.joinpath("NOTES.md").write_text(notes, encoding="utf-8")
