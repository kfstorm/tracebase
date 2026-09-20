"""Shared deterministic Markdown rendering and publication."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .context import ContextError, ContextExtractionResult, ContextItem
from .context_adapter import RenderedContextItem
from .context_adapters import CONTEXT_ADAPTERS
from .mutable_state import observation_json


def write_markdown(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def format_timestamp(value: Any, timezone: Any) -> str | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone).strftime("%Y-%m-%d %H:%M")


def _render_item(
    staging: Path, item: ContextItem, result: ContextExtractionResult
) -> RenderedContextItem:
    item_root = staging.joinpath(*PurePosixPath(item.path).parts)
    item_root.mkdir(parents=True, exist_ok=True)
    return item.adapter.render(item, result, item_root)


def _ordered_items(
    items: list[tuple[ContextItem, RenderedContextItem]],
) -> list[tuple[ContextItem, RenderedContextItem]]:
    rendered_by_adapter = {
        adapter: [
            (item, rendered) for item, rendered in items if item.adapter is adapter
        ]
        for adapter in CONTEXT_ADAPTERS
    }
    ordered: list[tuple[ContextItem, RenderedContextItem]] = []
    for adapter in CONTEXT_ADAPTERS:
        adapter_items = rendered_by_adapter[adapter]
        grouped: dict[str | None, list[tuple[ContextItem, RenderedContextItem]]] = {}
        for item, rendered in adapter_items:
            grouped.setdefault(rendered.ordering.group, []).append((item, rendered))
        ordered.extend(
            sorted(grouped.pop(None, []), key=lambda pair: pair[1].ordering.sort_key)
        )
        for group in sorted(grouped, key=lambda value: value or ""):
            ordered.extend(
                sorted(grouped[group], key=lambda pair: pair[1].ordering.sort_key)
            )
    return ordered


def _write_inventory(
    staging: Path,
    result: ContextExtractionResult,
    items: list[tuple[ContextItem, RenderedContextItem]],
) -> None:
    """Write the authoritative host-side Context manifest."""
    inventory = {
        "requested_interval": {
            "from": result.request.from_text,
            "to": result.request.to_text,
        },
        "items": [
            {
                "root": item.path,
                "files": list(rendered.files),
            }
            for item, rendered in items
        ],
    }
    (staging / "index.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_mutable_state(
    staging: Path,
    items: list[tuple[ContextItem, RenderedContextItem]],
) -> None:
    """Write host-only observations in the same order as the inventory."""
    metadata = {
        "items": [
            {
                "root": item.path,
                "observations": [
                    observation_json(observation)
                    for observation in rendered.mutable_state_observations
                ],
            }
            for item, rendered in items
        ]
    }
    (staging / "mutable-state.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _cleanup_staging(staging: Path) -> None:
    if not staging.exists():
        return
    try:
        shutil.rmtree(staging)
    except OSError:
        raise ContextError("context output cleanup failed") from None


def render_context(result: ContextExtractionResult, output: str | Path) -> Path:
    """Render completely, then publish the caller-owned output with one rename."""
    target = Path(output).absolute()
    staging: Path | None = None
    try:
        try:
            if target.exists() or target.is_symlink():
                raise ContextError("context output already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
            )
        except ContextError:
            raise
        except OSError:
            raise ContextError("context output publication failed") from None
        assert staging is not None
        try:
            rendered: list[tuple[ContextItem, RenderedContextItem]] = []
            for item in result.items:
                item_rendered = _render_item(staging, item, result)
                rendered.append((item, item_rendered))
            ordered = _ordered_items(rendered)
            _write_inventory(staging, result, ordered)
            _write_mutable_state(staging, ordered)
        except ContextError:
            raise
        except KeyError, TypeError, UnicodeError, ValueError, OSError:
            raise ContextError("context output rendering failed") from None
        try:
            staging.rename(target)
        except FileExistsError:
            raise ContextError("context output already exists") from None
        except OSError:
            raise ContextError("context output publication failed") from None
        return target
    finally:
        if staging is not None:
            _cleanup_staging(staging)
