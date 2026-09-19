"""Deterministic host-side planning for Summary execution shards."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .context_inventory import (
    ContextInventory,
    ContextInventoryError,
    item_readable_sizes,
    load_context_inventory,
)


@dataclass(frozen=True, slots=True)
class ShardPolicy:
    """Authoritative generic execution limits for every Context source."""

    max_bytes: int = 64 * 1024
    max_items: int = 8


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


@dataclass(slots=True)
class _DirectoryNode:
    children: dict[str, _DirectoryNode] = field(default_factory=dict)
    item_root: str | None = None


def _build_tree(inventory: ContextInventory) -> _DirectoryNode:
    root = _DirectoryNode()
    for item in inventory.items:
        node = root
        for part in PurePosixPath(item.root).parts:
            node = node.children.setdefault(part, _DirectoryNode())
        node.item_root = item.root
    return root


def _node_items(
    node: _DirectoryNode,
    positions: dict[str, int],
) -> tuple[str, ...]:
    if node.item_root is not None:
        return (node.item_root,)
    items: list[str] = []
    for child in node.children.values():
        items.extend(_node_items(child, positions))
    return tuple(sorted(items, key=positions.__getitem__))


def _unit(items: tuple[str, ...], sizes: dict[str, int]) -> _PlanningUnit:
    return _PlanningUnit(
        items,
        sum(sizes[item] for item in items),
    )


def _fits(unit: _PlanningUnit, policy: ShardPolicy) -> bool:
    return (
        unit.readable_bytes <= policy.max_bytes and len(unit.items) <= policy.max_items
    )


def _pack(
    units: tuple[_PlanningUnit, ...],
    policy: ShardPolicy,
    positions: dict[str, int],
) -> tuple[_PlanningUnit, ...]:
    """Pack fitting units in deterministic inventory order."""
    packed: list[_PlanningUnit] = []
    current: list[str] = []
    current_bytes = 0
    for unit in units:
        if current and (
            len(current) + len(unit.items) > policy.max_items
            or current_bytes + unit.readable_bytes > policy.max_bytes
        ):
            packed.append(
                _PlanningUnit(
                    tuple(sorted(current, key=positions.__getitem__)), current_bytes
                )
            )
            current = []
            current_bytes = 0
        current.extend(unit.items)
        current_bytes += unit.readable_bytes
    if current:
        packed.append(
            _PlanningUnit(
                tuple(sorted(current, key=positions.__getitem__)), current_bytes
            )
        )
    return tuple(packed)


def _plan_node(
    node: _DirectoryNode,
    sizes: dict[str, int],
    positions: dict[str, int],
    policy: ShardPolicy,
) -> tuple[tuple[_PlanningUnit, ...], bool]:
    items = _node_items(node, positions)
    intact = _unit(items, sizes)
    if _fits(intact, policy):
        return (intact,), True
    if node.item_root is not None:
        return (intact,), False

    isolated: list[_PlanningUnit] = []
    packable: list[_PlanningUnit] = []
    children = sorted(
        node.children.values(),
        key=lambda child: min(
            positions[item] for item in _node_items(child, positions)
        ),
    )
    for child in children:
        child_units, child_fits = _plan_node(child, sizes, positions, policy)
        if child_fits:
            packable.extend(child_units)
        else:
            isolated.extend(child_units)

    units = [*isolated, *_pack(tuple(packable), policy, positions)]
    units.sort(key=lambda unit: min(positions[item] for item in unit.items))
    return tuple(
        _PlanningUnit(unit.items, unit.readable_bytes) for unit in units
    ), False


def plan_shards(
    context_dir: Path, policy: ShardPolicy = SHARD_POLICY
) -> tuple[PlannedShard, ...]:
    """Create a deterministic tree partition with inventory-ordered units.

    Intact siblings may be packed around isolated overflow units, so the
    flattened shard sequence need not reproduce the global inventory order.
    """
    try:
        inventory = load_context_inventory(context_dir)
        sizes = item_readable_sizes(context_dir, inventory)
    except ContextInventoryError as error:
        raise ValueError(str(error)) from None

    positions = {item.root: index for index, item in enumerate(inventory.items)}
    if not inventory.items:
        return ()
    units, _root_fits = _plan_node(_build_tree(inventory), sizes, positions, policy)
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
