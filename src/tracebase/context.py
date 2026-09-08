"""Offline Context Output extraction from the published Raw Archive."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive import (
    FORMAT_VERSION,
    ArchiveError,
    CollectionRange,
    _archive_relative_path,
    _is_regular_file,
    encode_path_id,
)

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|z|[+-]\d{2}:\d{2})$"
)
_TIMESTAMP_KEYS = {
    "created_at",
    "updated_at",
    "timestamp",
    "time",
    "started_at",
    "ended_at",
    "completed_at",
    "createdAt",
    "updatedAt",
    "startedAt",
    "completedAt",
}


class ContextError(ArchiveError):
    """Raised for an invalid context request, archive, or publication."""


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
class LoadedSnapshot:
    run: dict[str, Any]
    manifest: dict[str, Any]
    evidence: dict[str, bytes]
    root: Path


@dataclass(frozen=True, slots=True)
class LoadedRun:
    manifest: dict[str, Any]
    snapshots: tuple[LoadedSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ContextItem:
    source: LoadedSnapshot
    reasons: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    path: str


@dataclass(frozen=True, slots=True)
class ContextExtractionResult:
    request: ContextRequest
    runs: tuple[LoadedRun, ...]
    items: tuple[ContextItem, ...]
    gaps: tuple[dict[str, str], ...]


def _json_file(path: Path, label: str) -> dict[str, Any]:
    if not _is_regular_file(path):
        raise ContextError(f"{label} is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        raise ContextError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise ContextError(f"{label} is invalid")
    return value


def _inside(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise ContextError("archive contains a symlink")
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        raise ContextError("archive path escapes its boundary") from None


def _load_snapshot(run_root: Path, run: dict[str, Any], entry: Any) -> LoadedSnapshot:
    if not isinstance(entry, dict):
        raise ContextError("run snapshot entry is invalid")
    try:
        object_kind = entry["object_kind"]
        source_kind = entry["source_kind"]
        source_id = entry["source_id"]
        relative = _archive_relative_path(entry["path"])
    except KeyError, ArchiveError:
        raise ContextError("run snapshot entry is invalid") from None
    if source_kind != run["source"]["kind"] or not all(
        isinstance(value, str) and value for value in (object_kind, source_id)
    ):
        raise ContextError("snapshot identity is inconsistent")
    root: Path = run_root.joinpath(*relative.parts)
    _inside(root, run_root / "snapshots")
    if not root.is_dir() or root.is_symlink():
        raise ContextError("snapshot directory is not regular")
    manifest = _json_file(root / "snapshot.json", "snapshot manifest")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ContextError("unsupported snapshot schema")
    if {
        manifest.get("source_kind"),
        manifest.get("object_kind"),
        manifest.get("source_id"),
    } != {source_kind, object_kind, source_id}:
        raise ContextError("snapshot identity is inconsistent")
    if not isinstance(manifest.get("observation_window"), dict):
        raise ContextError("snapshot observation window is invalid")
    declared = manifest.get("evidence_files")
    if not isinstance(declared, list):
        raise ContextError("snapshot evidence files are invalid")
    evidence: dict[str, bytes] = {}
    for descriptor in declared:
        if not isinstance(descriptor, dict) or not isinstance(
            descriptor.get("path"), str
        ):
            raise ContextError("snapshot evidence declaration is invalid")
        path = _archive_relative_path(descriptor["path"])
        if path == PurePosixPath("snapshot.json") or path.as_posix() in evidence:
            raise ContextError("snapshot evidence path is invalid")
        file_path: Path = root.joinpath(*path.parts)
        _inside(file_path, root)
        if not _is_regular_file(file_path):
            raise ContextError("declared evidence file is not regular")
        try:
            evidence[path.as_posix()] = file_path.read_bytes()
        except OSError:
            raise ContextError("declared evidence file is unreadable") from None
    actual: set[str] = set()
    for child in root.rglob("*"):
        _inside(child, root)
        if child == root / "snapshot.json" or child.is_dir():
            continue
        if not _is_regular_file(child):
            raise ContextError("snapshot contains a non-regular file")
        actual.add(child.relative_to(root).as_posix())
    if actual != set(evidence):
        raise ContextError("snapshot evidence does not match its manifest")
    return LoadedSnapshot(run, manifest, evidence, root)


def load_archive(root: str | Path) -> tuple[LoadedRun, ...]:
    """Load and validate every published run without consulting external sources."""
    archive = Path(root).absolute()
    runs_root = archive / "runs"
    if not runs_root.exists():
        return ()
    if not runs_root.is_dir() or runs_root.is_symlink():
        raise ContextError("published runs root is not a regular directory")
    loaded: list[LoadedRun] = []
    for run_root in sorted(runs_root.iterdir(), key=lambda path: path.name):
        _inside(run_root, runs_root)
        if not run_root.is_dir() or run_root.is_symlink():
            raise ContextError("published run is not a regular directory")
        run = _json_file(run_root / "run.json", "run manifest")
        if (
            run.get("format_version") != FORMAT_VERSION
            or run.get("run_id") != run_root.name
        ):
            raise ContextError("run manifest identity is invalid")
        source = run.get("source")
        if not isinstance(source, dict) or source.get("kind") not in {
            "github",
            "opencode",
        }:
            raise ContextError("unsupported source schema")
        if not isinstance(source.get("scope_id"), str) or not source["scope_id"]:
            raise ContextError("run source identity is invalid")
        collection = run.get("collection_range")
        if (
            not isinstance(collection, dict)
            or not isinstance(collection.get("from"), str)
            or not isinstance(collection.get("to"), str)
        ):
            raise ContextError("run collection range is invalid")
        try:
            CollectionRange.parse(collection["from"], collection["to"])
        except ArchiveError:
            raise ContextError("run collection range is invalid") from None
        snapshots_root = run_root / "snapshots"
        if not snapshots_root.is_dir() or snapshots_root.is_symlink():
            raise ContextError("run snapshot root is invalid")
        entries = run.get("snapshots")
        if not isinstance(entries, list):
            raise ContextError("run snapshots are invalid")
        listed_paths: set[str] = set()
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                if entry["path"] in listed_paths:
                    raise ContextError("run snapshot paths are not unique")
                listed_paths.add(entry["path"])
        actual_snapshot_paths = {
            path.parent.relative_to(run_root).as_posix()
            for path in snapshots_root.glob("*/*/snapshot.json")
            if path.is_file() and not path.is_symlink()
        }
        if actual_snapshot_paths != listed_paths:
            raise ContextError("run snapshots do not match its manifest")
        snapshots = tuple(_load_snapshot(run_root, run, entry) for entry in entries)
        loaded.append(LoadedRun(run, snapshots))
    return tuple(loaded)


def _timestamps(value: Any) -> list[datetime]:
    found: list[datetime] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key in _TIMESTAMP_KEYS
                and isinstance(child, str)
                and _TIMESTAMP.fullmatch(child)
            ):
                try:
                    found.append(ContextRequest._endpoint(child))
                except ContextError:
                    continue
            found.extend(_timestamps(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_timestamps(child))
    return found


def _snapshot_timestamps(snapshot: LoadedSnapshot) -> list[datetime]:
    timestamps: list[datetime] = []
    for name, content in snapshot.evidence.items():
        if not name.endswith(".json"):
            continue
        try:
            timestamps.extend(_timestamps(json.loads(content)))
        except UnicodeError, json.JSONDecodeError:
            continue
    return timestamps


def extract_context(
    request: ContextRequest, runs: tuple[LoadedRun, ...]
) -> ContextExtractionResult:
    """Build a disposable result whose items directly reference loaded snapshots."""
    items: list[ContextItem] = []
    gaps: list[dict[str, str]] = []
    for run in runs:
        for snapshot in run.snapshots:
            timestamps = _snapshot_timestamps(snapshot)
            selected = [
                value for value in timestamps if request.start <= value < request.end
            ]
            if not selected:
                continue
            roles: tuple[str, ...] = ("in_range_record",)
            if any(value < request.start for value in timestamps):
                roles += ("prior_background",)
            relative = (
                f"{snapshot.manifest['source_kind']}/{encode_path_id(run.manifest['source']['scope_id'])}/"
                f"{snapshot.manifest['object_kind']}/{encode_path_id(snapshot.manifest['source_id'])}"
            )
            reasons = ("source_record_in_range",)
            items.append(ContextItem(snapshot, reasons, roles, relative))
    items.sort(key=lambda item: (item.path, item.source.run["run_id"]))
    return ContextExtractionResult(request, runs, tuple(items), tuple(gaps))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def render_context(result: ContextExtractionResult, output: str | Path) -> Path:
    """Render completely, then publish the caller-owned output with one rename."""
    target = Path(output).absolute()
    if target.exists() or target.is_symlink():
        raise ContextError("context output already exists")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    try:
        item_manifests: list[dict[str, Any]] = []
        for item in result.items:
            item_root = staging.joinpath(*PurePosixPath(item.path).parts)
            item_root.mkdir(parents=True, exist_ok=True)
            for name, content in sorted(item.source.evidence.items()):
                destination = item_root.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            item_manifests.append(
                {
                    "source_kind": item.source.manifest["source_kind"],
                    "source_scope_id": item.source.run["source"]["scope_id"],
                    "object_kind": item.source.manifest["object_kind"],
                    "source_id": item.source.manifest["source_id"],
                    "inclusion_reasons": list(item.reasons),
                    "temporal_roles": list(item.temporal_roles),
                    "path": item.path,
                    "provenance": {
                        "run_id": item.source.run["run_id"],
                        "snapshot": item.source.manifest,
                    },
                }
            )
        inventory = sorted(
            path.relative_to(staging).as_posix()
            for path in staging.rglob("*")
            if path.is_file()
        )
        context = {
            "schema_version": 1,
            "request": {"from": result.request.from_text, "to": result.request.to_text},
            "source_items": item_manifests,
            "relations": [],
            "gaps": list(result.gaps),
            "output_inventory": [*inventory, "context.json", "index.md"],
        }
        _write_json(staging / "context.json", context)
        lines = [
            "# Context Output",
            "",
            f"Range: [{result.request.from_text}, {result.request.to_text})",
            "",
            "## Source Items",
            "",
        ]
        if not item_manifests:
            lines.append("No source items were selected.")
        else:
            lines.extend(
                f"- `{manifest_item['path']}/` "
                f"({', '.join(manifest_item['inclusion_reasons'])})"
                for manifest_item in item_manifests
            )
        lines.extend(
            [
                "",
                "## Gaps",
                "",
                "No gaps recorded."
                if not result.gaps
                else "- Archive or source uncertainty is recorded in `context.json`.",
                "",
            ]
        )
        (staging / "index.md").write_text("\n".join(lines), encoding="utf-8")
        staging.rename(target)
        return target
    except (OSError, TypeError, ValueError) as error:
        raise ContextError("context output publication failed") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    runs = load_archive(archive)
    return render_context(extract_context(request, runs), output)
