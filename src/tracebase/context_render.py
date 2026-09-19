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


def _link_list(paths: list[str]) -> str:
    return ", ".join(f"[{PurePosixPath(path).name}]({path})" for path in paths)


def _render_index(
    staging: Path,
    result: ContextExtractionResult,
    items: list[tuple[ContextItem, RenderedContextItem]],
) -> list[tuple[ContextItem, RenderedContextItem]]:
    lines = [
        "# Context Output",
        "",
        "Requested interval: "
        f"`{result.request.from_text} <= t < {result.request.to_text}`",
        "Times are displayed in the requested interval offset.",
        "Attribution mode is declared for every source item; Summary projects "
        "user work before synthesizing workstreams.",
        "",
    ]
    rendered_by_adapter = {
        adapter: [
            (item, rendered) for item, rendered in items if item.adapter is adapter
        ]
        for adapter in CONTEXT_ADAPTERS
    }
    ordered: list[tuple[ContextItem, RenderedContextItem]] = []
    for adapter_index, adapter in enumerate(CONTEXT_ADAPTERS):
        lines.extend([f"## {adapter.index_section}", ""])
        adapter_items = rendered_by_adapter[adapter]
        source_items = tuple(item for item, _rendered in adapter_items)
        if not adapter_items:
            lines.append(adapter.empty_index_message)
        else:
            lines.extend(adapter.index_header(source_items))
            grouped: dict[
                str | None, list[tuple[ContextItem, RenderedContextItem]]
            ] = {}
            for item, rendered in adapter_items:
                group = rendered.index.group
                grouped.setdefault(group, []).append((item, rendered))
            for item, rendered in sorted(
                grouped.pop(None, []), key=lambda pair: pair[1].index.sort_key
            ):
                _append_index_item(lines, item, rendered)
                ordered.append((item, rendered))
            named_groups = [group for group in grouped if group is not None]
            for group in sorted(named_groups):
                lines.extend([f"### `{group}`", ""])
                for item, rendered in sorted(
                    grouped[group], key=lambda pair: pair[1].index.sort_key
                ):
                    _append_index_item(lines, item, rendered)
                    ordered.append((item, rendered))
                lines.append("")
        if adapter_index < len(CONTEXT_ADAPTERS) - 1:
            lines.append("")
    lines.append("")
    write_markdown(staging / "index.md", lines)
    return ordered


def _write_inventory(
    staging: Path, items: list[tuple[ContextItem, RenderedContextItem]]
) -> None:
    """Write the authoritative orchestration metadata in human index order."""
    inventory = {
        "items": [
            {
                "root": item.path,
                "attribution_mode": item.attribution_mode.value,
                "group": rendered.index.group,
                "files": list(rendered.files),
            }
            for item, rendered in items
        ]
    }
    (staging / "index.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _append_index_item(
    lines: list[str], item: ContextItem, rendered: RenderedContextItem
) -> None:
    """Add a source-neutral index item from adapter-provided metadata."""
    files = rendered.files
    links = [f"{item.path}/{name}" for name in files]
    lines.append(
        f"- **{rendered.index.label}** [attribution mode: "
        f"`{item.attribution_mode.value}`] ({_link_list(links)})"
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
            ordered = _render_index(staging, result, rendered)
            _write_inventory(staging, ordered)
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
