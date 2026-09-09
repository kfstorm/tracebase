"""Offline Context Output generation from the published Raw Archive."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive import (
    ArchiveError,
    PublishedRun,
    PublishedSnapshot,
    encode_path_id,
    load_published_archive,
)
from .github_context import GitHubProjection, project_github
from .opencode_context import (
    OpenCodeProjection,
    project_opencode,
    resolve_opencode_context,
)

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|z|[+-]\d{2}:\d{2})$"
)
_SUPPORTED_CONTEXT_OBJECT_KINDS = {
    "github": {"issue", "pull-request"},
    "opencode": {"session"},
}


class ContextError(ArchiveError):
    """Raised when context generation cannot satisfy its public contract."""


@dataclass(frozen=True, slots=True)
class ContextRequest:
    from_text: str
    to_text: str
    start: datetime
    end: datetime

    @classmethod
    def parse(cls, from_text: str, to_text: str) -> ContextRequest:
        start = cls._endpoint(from_text)
        end = cls._endpoint(to_text)
        if start >= end:
            raise ContextError("context range requires from to be before to")
        return cls(from_text, to_text, start, end)

    @staticmethod
    def _endpoint(value: str) -> datetime:
        if (
            isinstance(value, str)
            and "T" in value
            and not re.search(r"(?:Z|z|[+-]\d{2}:\d{2})$", value)
        ):
            raise ContextError("context endpoints require an explicit offset")
        if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
            raise ContextError(
                "context endpoints require ISO 8601 offsets and 0-6 fractional digits"
            )
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError:
            raise ContextError(
                "context endpoint is not a valid ISO 8601 timestamp"
            ) from None
        if result.tzinfo is None or result.utcoffset() is None:
            raise ContextError("context endpoints require an explicit offset")
        return result


@dataclass(frozen=True, slots=True)
class ContextItem:
    snapshots: tuple[PublishedSnapshot, ...]
    path: str
    github: GitHubProjection | None = None
    opencode: OpenCodeProjection | None = None

    @property
    def source(self) -> PublishedSnapshot:
        return self.snapshots[0]


@dataclass(frozen=True, slots=True)
class ContextExtractionResult:
    request: ContextRequest
    runs: tuple[PublishedRun, ...]
    items: tuple[ContextItem, ...]


def load_archive(root: str | Path) -> tuple[PublishedRun, ...]:
    """Load the current Raw Archive without inspecting provider payload semantics."""
    try:
        return load_published_archive(root)
    except ArchiveError as error:
        raise ContextError(str(error)) from None


def _logical_key(snapshot: PublishedSnapshot) -> tuple[str, str, str, str]:
    return (
        snapshot.manifest["source_kind"],
        snapshot.run["source"]["scope_id"],
        snapshot.manifest["object_kind"],
        snapshot.manifest["source_id"],
    )


def _item_path(key: tuple[str, str, str, str]) -> str:
    return f"{key[0]}/{encode_path_id(key[1])}/{key[2]}/{encode_path_id(key[3])}"


def _validate_context_source(snapshot: PublishedSnapshot) -> None:
    source_kind = snapshot.manifest["source_kind"]
    supported_kinds = _SUPPORTED_CONTEXT_OBJECT_KINDS.get(source_kind)
    if (
        supported_kinds is None
        or snapshot.manifest["object_kind"] not in supported_kinds
    ):
        raise ContextError("unsupported context source")


def extract_context(
    request: ContextRequest, runs: tuple[PublishedRun, ...]
) -> ContextExtractionResult:
    """Group all supported Archive objects without source-record interpretation."""
    grouped: dict[tuple[str, str, str, str], list[PublishedSnapshot]] = {}
    for run in runs:
        for snapshot in run.snapshots:
            _validate_context_source(snapshot)
            grouped.setdefault(_logical_key(snapshot), []).append(snapshot)
    all_items: list[ContextItem] = []
    opencode_projections: dict[tuple[str, str, str, str], OpenCodeProjection] = {}
    for key, snapshots in sorted(grouped.items()):
        ordered = tuple(sorted(snapshots, key=lambda value: value.run["run_id"]))
        try:
            github = (
                project_github(ordered, request.start, request.end)
                if key[0] == "github"
                else None
            )
            opencode = (
                project_opencode(ordered, request.start, request.end)
                if key[0] == "opencode"
                else None
            )
        except ArchiveError as error:
            raise ContextError(str(error)) from None
        if opencode is not None:
            opencode_projections[key] = opencode
        all_items.append(ContextItem(ordered, _item_path(key), github, opencode))
    projections_by_scope: dict[
        str, list[tuple[tuple[str, str, str, str], OpenCodeProjection]]
    ] = {}
    for key, projection in opencode_projections.items():
        projections_by_scope.setdefault(key[1], []).append((key, projection))
    for scoped in projections_by_scope.values():
        resolved = resolve_opencode_context(
            tuple(projection for _, projection in scoped)
        )
        for (key, _), projection in zip(scoped, resolved, strict=True):
            opencode_projections[key] = projection
    supporting_keys = {
        key
        for key, projection in opencode_projections.items()
        if projection.inclusion_reasons == ("supporting-task-context",)
    }
    all_items = [
        replace(
            item,
            opencode=opencode_projections.get(_logical_key(item.source)),
        )
        for item in all_items
    ]
    items = tuple(
        item
        for item in all_items
        if (item.github is None or item.github.selected)
        and (
            item.opencode is None
            or item.opencode.selected
            or _logical_key(item.source) in supporting_keys
        )
    )
    return ContextExtractionResult(
        request,
        runs,
        items,
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _cleanup_staging(staging: Path) -> None:
    if not staging.exists():
        return
    try:
        shutil.rmtree(staging)
    except OSError:
        raise ContextError("context output cleanup failed") from None


def _observation_name(index: int, run_id: str) -> str:
    return f"{index:03d}-{run_id}"


def _github_records_for_output(item: ContextItem) -> tuple[dict[str, Any], ...]:
    if item.github is None:
        return ()
    observation_paths = {
        snapshot.run["run_id"]: (
            f"observations/{_observation_name(index, snapshot.run['run_id'])}"
        )
        for index, snapshot in enumerate(item.snapshots, start=1)
    }
    records: list[dict[str, Any]] = []
    for record in item.github.records:
        representations = []
        for representation in record["representations"]:
            output_path = (
                f"{observation_paths[representation['run_id']]}/"
                f"{representation['evidence_path']}"
            )
            representations.append({**representation, "output_path": output_path})
        records.append({**record, "representations": representations})
    return tuple(records)


def _render_source_view(
    item_root: Path,
    item: ContextItem,
    github_records: tuple[dict[str, Any], ...] = (),
) -> str:
    """Render the shared source-view shell; source projections extend this seam."""
    source = item.source
    if item.github is not None:
        projection = item.github
        _write_json(
            item_root / "github.json",
            {
                "schema_version": 1,
                "inclusion_reasons": projection.inclusion_reasons,
                "temporal_roles": projection.temporal_roles,
                "records": github_records,
                "relations": projection.relations,
            },
        )
        lines = ["# GitHub Item", "", "## Source-native Records", ""]
        for record in github_records:
            links = ", ".join(
                f"[{representation['evidence_path']}]({representation['output_path']})"
                for representation in record["representations"]
            )
            lines.append(
                f"- `{record['kind']}` `{record['native_id']}` "
                f"({', '.join(record['temporal_roles'])}; {links})"
            )
        lines.extend(["", "## Observation State", ""])
        lines.append(
            "Review thread state is observed current state, not proof of a fix."
        )
        lines.append("Aggregate diffs do not establish a fix or fixing commit.")
        (item_root / "github.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"{item.path}/github.md"
    if item.opencode is not None:
        opencode_projection = item.opencode
        _write_json(
            item_root / "opencode.json",
            {
                "schema_version": 1,
                "session": opencode_projection.session,
                "messages": opencode_projection.messages,
                "inclusion_reasons": opencode_projection.inclusion_reasons,
                "temporal_roles": opencode_projection.temporal_roles,
                "gaps": opencode_projection.gaps,
            },
        )
        lines = ["# OpenCode Session", "", "## Messages", ""]
        for message in opencode_projection.messages:
            roles = ", ".join(message["temporal_roles"]) or "observed_state"
            lines.append(f"- `{message['id']}` ({roles})")
            for part in message.get("parts", ()):
                roles = ", ".join(part["temporal_roles"]) or "observed_state"
                lines.append(f"  - {part['type']} `{part['id']}` ({roles})")
        (item_root / "opencode.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return f"{item.path}/opencode.md"
    view_path = f"{item.path}/{source.manifest['source_kind']}.md"
    lines = [
        "# Source Item",
        "",
        f"- Source kind: `{source.manifest['source_kind']}`",
        f"- Object kind: `{source.manifest['object_kind']}`",
        f"- Source ID: `{source.manifest['source_id']}`",
        f"- Scope ID: `{source.run['source']['scope_id']}`",
        "",
        "## Source-native Evidence",
        "",
    ]
    for observation_index, snapshot in enumerate(item.snapshots, start=1):
        observation_name = _observation_name(observation_index, snapshot.run["run_id"])
        lines.extend(
            f"- [{name}](observations/{observation_name}/{name})"
            for name in sorted(snapshot.evidence)
        )
    (item_root / f"{source.manifest['source_kind']}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return view_path


def _render_item(staging: Path, item: ContextItem) -> dict[str, Any]:
    item_root = staging.joinpath(*PurePosixPath(item.path).parts)
    item_root.mkdir(parents=True, exist_ok=True)
    provenance: list[dict[str, Any]] = []
    for observation_index, snapshot in enumerate(item.snapshots, start=1):
        run_id = snapshot.run["run_id"]
        observation_name = _observation_name(observation_index, run_id)
        observation_root = item_root / "observations" / observation_name
        for name, content in sorted(snapshot.evidence.items()):
            destination = observation_root.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        provenance.append(
            {
                "run_id": run_id,
                "source": snapshot.run["source"],
                "collection_range": snapshot.run["collection_range"],
                "snapshot": snapshot.manifest,
                "output_path": f"{item.path}/observations/{observation_name}",
            }
        )
    source = item.source
    github_records = _github_records_for_output(item)
    view_path = _render_source_view(item_root, item, github_records)
    result = {
        "source_kind": source.manifest["source_kind"],
        "source_scope_id": source.run["source"]["scope_id"],
        "object_kind": source.manifest["object_kind"],
        "source_id": source.manifest["source_id"],
        "path": item.path,
        "view_path": view_path,
        "provenance": provenance,
    }
    if item.github is not None:
        result["inclusion_reasons"] = item.github.inclusion_reasons
        result["temporal_roles"] = item.github.temporal_roles
        result["github_path"] = f"{item.path}/github.json"
    if item.opencode is not None:
        result["inclusion_reasons"] = item.opencode.inclusion_reasons
        result["temporal_roles"] = item.opencode.temporal_roles
        result["opencode_path"] = f"{item.path}/opencode.json"
    return result


def _render_index(
    staging: Path, result: ContextExtractionResult, items: list[dict[str, Any]]
) -> None:
    lines = [
        "# Context Output",
        "",
        f"Range: [{result.request.from_text}, {result.request.to_text})",
        "",
        "## Source Items",
        "",
    ]
    if items:
        lines.extend(
            f"- `{item['path']}/` [{item['source_kind']} view]({item['view_path']})"
            + (
                f": {', '.join(item['inclusion_reasons'])}; "
                f"{', '.join(item['temporal_roles'])}"
                if "inclusion_reasons" in item
                else ""
            )
            for item in items
        )
    else:
        lines.append("No source items are available.")
    (staging / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_manifest(
    staging: Path, result: ContextExtractionResult, items: list[dict[str, Any]]
) -> None:
    inventory = sorted(
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    )
    _write_json(
        staging / "context.json",
        {
            "schema_version": 1,
            "request": {"from": result.request.from_text, "to": result.request.to_text},
            "source_items": items,
            "relations": [],
            "unresolved_references": [],
            "gaps": [],
            "output_inventory": [*inventory, "context.json"],
        },
    )


def render_context(result: ContextExtractionResult, output: str | Path) -> Path:
    """Render completely, then publish the caller-owned output with one rename."""
    target = Path(output).absolute()
    if target.exists() or target.is_symlink():
        raise ContextError("context output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        items = [_render_item(staging, item) for item in result.items]
        _render_index(staging, result, items)
        _render_manifest(staging, result, items)
        staging.rename(target)
        return target
    except OSError, TypeError, ValueError:
        raise ContextError("context output publication failed") from None
    finally:
        _cleanup_staging(staging)


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    return render_context(extract_context(request, load_archive(archive)), output)
