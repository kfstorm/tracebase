"""Machine-readable Context inventory and tree validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .attribution import AttributionMode

_INVENTORY_FIELDS = {"root", "attribution_mode", "group", "files"}


class ContextInventoryError(ValueError):
    """Raised when Context inventory or its corresponding tree is invalid."""


@dataclass(frozen=True, slots=True)
class ContextInventoryItem:
    """The generic orchestration metadata for one Context item."""

    root: str
    attribution_mode: AttributionMode
    group: str | None
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextInventory:
    """The authoritative, stable-order Context item inventory."""

    items: tuple[ContextInventoryItem, ...]


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ContextInventoryError(f"{label} must be a relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ContextInventoryError(f"{label} must be a relative POSIX path")
    return value


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ContextInventoryError("Context inventory must be a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        raise ContextInventoryError("Context inventory is not valid JSON") from None


def load_context_inventory(context_dir: Path) -> ContextInventory:
    """Load and validate inventory metadata and its referenced Context tree."""
    if context_dir.is_symlink() or not context_dir.is_dir():
        raise ContextInventoryError("Context Output must be a regular directory")

    raw = _load_json(context_dir / "index.json")
    if not isinstance(raw, dict) or set(raw) != {"items"}:
        raise ContextInventoryError("Context inventory must contain only an items list")
    raw_items = raw["items"]
    if not isinstance(raw_items, list):
        raise ContextInventoryError("Context inventory items must be a list")

    items: list[ContextInventoryItem] = []
    roots: set[str] = set()
    context_root = context_dir.resolve(strict=False)
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or set(raw_item) != _INVENTORY_FIELDS:
            raise ContextInventoryError("Context inventory item has invalid fields")
        root = _relative_path(raw_item.get("root"), "Context inventory root")
        if root in roots:
            raise ContextInventoryError(
                f"Context inventory root {root!r} is duplicated"
            )
        roots.add(root)

        mode = raw_item.get("attribution_mode")
        if not isinstance(mode, str):
            raise ContextInventoryError(
                f"Context inventory attribution mode for {root!r} is invalid"
            )
        try:
            attribution_mode = AttributionMode(mode)
        except ValueError:
            raise ContextInventoryError(
                f"Context inventory attribution mode for {root!r} is invalid"
            ) from None

        group = raw_item.get("group")
        if group is not None and (not isinstance(group, str) or not group):
            raise ContextInventoryError(
                f"Context inventory group for {root!r} is invalid"
            )

        raw_files = raw_item.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ContextInventoryError(
                f"Context inventory files for {root!r} must be non-empty"
            )
        files: list[str] = []
        for raw_file in raw_files:
            file_path = _relative_path(raw_file, f"Context inventory file for {root!r}")
            if file_path in files:
                raise ContextInventoryError(
                    f"Context inventory file {file_path!r} is duplicated"
                )
            files.append(file_path)

        item_path = context_dir.joinpath(*PurePosixPath(root).parts)
        resolved_item = item_path.resolve(strict=False)
        if (
            item_path.is_symlink()
            or not item_path.is_dir()
            or (
                resolved_item != context_root
                and context_root not in resolved_item.parents
            )
        ):
            raise ContextInventoryError(f"Context item directory {root!r} is invalid")
        for path in item_path.rglob("*"):
            if path.is_symlink():
                raise ContextInventoryError(f"Context item {root!r} contains a symlink")
        for file_path in files:
            expected = item_path.joinpath(*PurePosixPath(file_path).parts)
            if expected.is_symlink() or not expected.is_file():
                raise ContextInventoryError(
                    f"Context item {root!r} is missing expected file {file_path!r}"
                )
        items.append(ContextInventoryItem(root, attribution_mode, group, tuple(files)))

    return ContextInventory(tuple(items))


def item_readable_sizes(
    context_dir: Path, inventory: ContextInventory
) -> dict[str, int]:
    """Measure all regular files below each validated item root."""
    sizes: dict[str, int] = {}
    for item in inventory.items:
        item_path = context_dir.joinpath(*PurePosixPath(item.root).parts)
        total = 0
        for path in item_path.rglob("*"):
            if path.is_symlink():
                raise ContextInventoryError(
                    f"Context item {item.root!r} contains a symlink"
                )
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    raise ContextInventoryError(
                        f"Context item {item.root!r} contains an unreadable file"
                    ) from None
        sizes[item.root] = total
    return sizes
