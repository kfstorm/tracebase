"""Machine-readable Context inventory and tree validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_INVENTORY_FIELDS = {"root", "files"}
_INVENTORY_TOP_LEVEL_FIELDS = {"requested_interval", "items"}


class ContextInventoryError(ValueError):
    """Raised when Context inventory or its corresponding tree is invalid."""


@dataclass(frozen=True, slots=True)
class ContextInventoryItem:
    """The generic orchestration metadata for one Context item."""

    root: str
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextInventory:
    """The authoritative, stable-order Context item inventory."""

    requested_interval: tuple[str, str]
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


def _reject_legacy_index(context_dir: Path) -> None:
    if (context_dir / "index.md").exists() or (context_dir / "index.md").is_symlink():
        raise ContextInventoryError("Context Output must not contain index.md")


def _validate_context_dir(context_dir: Path) -> None:
    if context_dir.is_symlink() or not context_dir.is_dir():
        raise ContextInventoryError("Context Output must be a regular directory")
    _reject_legacy_index(context_dir)


def load_context_inventory(context_dir: Path) -> ContextInventory:
    """Load and validate inventory metadata and its referenced Context tree."""
    _validate_context_dir(context_dir)

    raw = _load_json(context_dir / "index.json")
    if not isinstance(raw, dict) or set(raw) != _INVENTORY_TOP_LEVEL_FIELDS:
        raise ContextInventoryError(
            "Context inventory must contain requested_interval and items"
        )
    raw_interval = raw["requested_interval"]
    if not isinstance(raw_interval, dict) or set(raw_interval) != {"from", "to"}:
        raise ContextInventoryError(
            "Context inventory requested_interval must contain from and to"
        )
    from_text = raw_interval["from"]
    to_text = raw_interval["to"]
    if not isinstance(from_text, str) or not isinstance(to_text, str):
        raise ContextInventoryError(
            "Context inventory requested_interval values must be strings"
        )
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
        if any(
            root.startswith(existing + "/") or existing.startswith(root + "/")
            for existing in roots
        ):
            raise ContextInventoryError(
                f"Context inventory root {root!r} overlaps another item root"
            )
        roots.add(root)

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
        actual_files = {
            path.relative_to(item_path).as_posix()
            for path in item_path.rglob("*")
            if path.is_file()
        }
        if actual_files != set(files):
            if any(
                isinstance(other, dict)
                and isinstance(other.get("root"), str)
                and other["root"] != root
                and (
                    other["root"].startswith(root + "/")
                    or root.startswith(other["root"] + "/")
                )
                for other in raw_items
            ):
                raise ContextInventoryError(
                    f"Context inventory root {root!r} overlaps another item root"
                )
            raise ContextInventoryError(
                f"Context item {root!r} file inventory does not match manifest"
            )
        items.append(ContextInventoryItem(root, tuple(files)))

    return ContextInventory((from_text, to_text), tuple(items))


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
