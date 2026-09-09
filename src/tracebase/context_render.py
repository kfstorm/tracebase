"""Shared deterministic Markdown rendering and publication."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .context import ContextError, ContextExtractionResult, ContextItem


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def front_matter(fields: tuple[tuple[str, Any], ...]) -> list[str]:
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            lines.extend(f"  - {_quote(str(entry))}" for entry in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {_quote(str(value))}")
    lines.extend(["---", ""])
    return lines


def fenced(value: str, language: str) -> list[str]:
    fence = "```"
    while fence in value:
        fence += "`"
    return [f"{fence}{language}", value, fence]


def write_markdown(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _coverage_summary(coverage: Any) -> dict[str, Any]:
    if not isinstance(coverage, dict):
        return {}
    allowed = (
        "discovery_matrix_version",
        "listed_session_count",
        "pagination_complete",
        "permission_boundary",
        "selected_artifacts",
        "selected_session_count",
    )
    return {
        key: coverage[key]
        for key in allowed
        if key in coverage and isinstance(coverage[key], (str, int, bool))
    }


def render_provenance(lines: list[str], item: ContextItem) -> None:
    lines.extend(["## Provenance", ""])
    for index, snapshot in enumerate(item.snapshots, start=1):
        collection = snapshot.run["collection_range"]
        observation = snapshot.manifest["observation_window"]
        lines.extend(
            [
                f"### Observation {index}",
                "",
                "- Collection Run Coverage: "
                f"[{collection['from']}, {collection['to']})",
                "- Snapshot Observation Window: "
                f"[{observation['from']}, {observation['to']})",
            ]
        )
        coverage = _coverage_summary(snapshot.run.get("coverage"))
        if coverage:
            lines.append(
                f"- Coverage summary: `{json.dumps(coverage, sort_keys=True)}`"
            )
        lines.append("")


def item_front_matter(
    item: ContextItem,
    result: ContextExtractionResult,
    source_kind: str,
    projection: Any,
) -> list[str]:
    source = item.source
    return front_matter(
        (
            ("schema_version", 1),
            ("source_kind", source_kind),
            ("source_scope_id", source.run["source"]["scope_id"]),
            ("source_id", source.manifest["source_id"]),
            ("object_kind", source.manifest["object_kind"]),
            ("request_from", result.request.from_text),
            ("request_to", result.request.to_text),
            ("inclusion_reasons", projection.inclusion_reasons),
            ("temporal_roles", projection.temporal_roles),
        )
    )


def _root_gaps(items: tuple[ContextItem, ...]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    for item in items:
        if item.opencode is None:
            continue
        gaps.extend(
            {
                "source_kind": "opencode",
                "source_id": item.source.manifest["source_id"],
                **{key: str(value) for key, value in gap.items()},
            }
            for gap in item.opencode.gaps
        )
    return sorted(gaps, key=lambda gap: json.dumps(gap, sort_keys=True))


def _render_item(
    staging: Path, item: ContextItem, result: ContextExtractionResult
) -> str:
    item_root = staging.joinpath(*PurePosixPath(item.path).parts)
    item_root.mkdir(parents=True, exist_ok=True)
    if item.github is not None:
        from .github_context_render import render_github  # noqa: PLC0415

        lines = render_github(item, result)
        view_name = "github.md"
    elif item.opencode is not None:
        from .opencode_context_render import render_opencode  # noqa: PLC0415

        lines = render_opencode(item, result)
        view_name = "opencode.md"
    else:
        raise ContextError("context item has no source projection")
    view_path = f"{item.path}/{view_name}"
    write_markdown(item_root / view_name, lines)
    return view_path


def _render_index(
    staging: Path,
    result: ContextExtractionResult,
    items: list[tuple[ContextItem, str]],
) -> None:
    gaps = _root_gaps(result.items)
    lines = front_matter(
        (
            ("schema_version", 1),
            ("request_from", result.request.from_text),
            ("request_to", result.request.to_text),
        )
    )
    lines.extend(["# Context Output", "", "## Source Items", ""])
    if not items:
        lines.append("No source items are available.")
    for item, view_path in items:
        projection = item.github or item.opencode
        assert projection is not None
        source = item.source
        reasons = ", ".join(projection.inclusion_reasons) or "none"
        roles = ", ".join(projection.temporal_roles) or "observed_state"
        lines.append(
            f"- [{source.manifest['source_kind']}:{source.manifest['source_id']}]"
            f"({view_path}) - reasons: {reasons}; temporal roles: {roles}"
        )
    lines.extend(["", "## Gaps", ""])
    if not gaps:
        lines.append("No gaps are available.")
    else:
        for gap in gaps:
            kind = gap.get("kind", "unknown")
            source_id = gap.get("source_id", "unknown")
            lines.append(
                f"- `{kind}` for `{source_id}`: `{json.dumps(gap, sort_keys=True)}`"
            )
    write_markdown(staging / "index.md", lines)


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
            rendered = [
                (item, _render_item(staging, item, result)) for item in result.items
            ]
            _render_index(staging, result, rendered)
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
