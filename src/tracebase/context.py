"""Offline Context Output extraction from the published Raw Archive."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive import (
    FORMAT_VERSION,
    ArchiveError,
    CollectionRange,
    _archive_relative_path,
    _ensure_no_symlink,
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
_NUMERIC_TIMESTAMP_KEYS = {
    "created",
    "updated",
    "started",
    "ended",
    "completed",
    "start",
    "end",
}
_SNAPSHOT_PATH_PARTS = 3
_SUPPORTED_OBJECT_KINDS = {"github": {"issue", "pull-request"}, "opencode": {"session"}}
_GITHUB_REFERENCE = re.compile(
    r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)
_GITHUB_API_ITEM = re.compile(
    r"https://api\.github\.com/repos/([^/]+)/([^/]+)/(issues|pulls)/(\d+)$",
    re.IGNORECASE,
)


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
    snapshots: tuple[LoadedSnapshot, ...]
    reasons: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    path: str

    @property
    def source(self) -> LoadedSnapshot:
        """Return the first direct source reference for compatibility with readers."""
        return self.snapshots[0]


@dataclass(frozen=True, slots=True)
class ContextExtractionResult:
    request: ContextRequest
    runs: tuple[LoadedRun, ...]
    items: tuple[ContextItem, ...]
    gaps: tuple[dict[str, str], ...]
    relations: tuple[dict[str, Any], ...] = ()
    unresolved_references: tuple[dict[str, str], ...] = ()
    direct_relations: tuple[ContextRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextRelation:
    """An in-memory relation retaining direct loaded-object references."""

    kind: str
    source: ContextItem
    target: ContextItem | LoadedSnapshot | None
    occurrence: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _TemporalRecord:
    start: datetime
    end: datetime | None = None
    kind: str = "point"


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


def _timestamp_value(value: Any, numeric: bool = False) -> datetime | None:
    if numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
    if isinstance(value, str) and _TIMESTAMP.fullmatch(value):
        try:
            return ContextRequest._endpoint(value)
        except ContextError:
            return None
    return None


def _record_fields(value: dict[str, Any]) -> list[tuple[str, datetime]]:
    fields: list[tuple[str, datetime]] = []
    for key, child in value.items():
        timestamp = _timestamp_value(child, key in _NUMERIC_TIMESTAMP_KEYS)
        if timestamp is not None:
            fields.append((key, timestamp))
        elif key in {"time", "info", "state"} and isinstance(child, dict):
            fields.extend(_record_fields(child))
    return fields


def _is_actual_opencode_record(value: dict[str, Any]) -> bool:
    if {"role", "callID", "tool", "body", "text"} & value.keys():
        return True
    if isinstance(value.get("parts"), list) and value["parts"]:
        return True
    record_type = value.get("type")
    return isinstance(record_type, str) and record_type not in {
        "session",
        "session-export",
    }


def _is_context_only_opencode_record(value: dict[str, Any]) -> bool:
    record_type = value.get("type")
    return bool(
        value.get("synthetic") is True
        or value.get("isSynthetic") is True
        or record_type
        in {
            "compaction",
            "compaction-summary",
            "synthetic",
            "synthetic-continuation",
            "continuation",
        }
    )


def _opencode_records(value: Any) -> list[_TemporalRecord]:
    records: list[_TemporalRecord] = []
    if isinstance(value, dict):
        fields = _record_fields(value)
        if fields and _is_actual_opencode_record(value):
            if _is_context_only_opencode_record(value):
                records.extend(
                    _TemporalRecord(timestamp, kind="context")
                    for _, timestamp in fields
                )
            else:
                execution = bool(
                    {"tool", "callID"} & value.keys()
                    or value.get("type")
                    in {"tool", "tool-call", "tool_result", "function"}
                )
                end_keys = {
                    "end",
                    "ended",
                    "ended_at",
                    "completed",
                    "completed_at",
                    "completedAt",
                }
                start_keys = {
                    "start",
                    "started",
                    "started_at",
                    "startedAt",
                    "created",
                    "created_at",
                    "createdAt",
                    "timestamp",
                }
                start_fields = [
                    timestamp for key, timestamp in fields if key in start_keys
                ]
                end_fields = [timestamp for key, timestamp in fields if key in end_keys]
                if execution and start_fields:
                    records.append(
                        _TemporalRecord(
                            min(start_fields),
                            max(end_fields) if end_fields else None,
                            "execution",
                        )
                    )
                records.extend(
                    _TemporalRecord(timestamp)
                    for key, timestamp in fields
                    if key
                    not in {
                        "start",
                        "started",
                        "started_at",
                        "startedAt",
                        "end",
                        "ended",
                        "ended_at",
                        "completed",
                        "completed_at",
                        "completedAt",
                    }
                )
        for child in value.values():
            records.extend(_opencode_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_opencode_records(child))
    return records


def _ensure_within(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise ContextError("archive contains a symlink")
    try:
        _ensure_no_symlink(path, root)
    except ArchiveError:
        raise ContextError("archive contains a symlink") from None
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        raise ContextError("archive path escapes its boundary") from None


def _load_snapshot(  # noqa: PLR0915
    run_root: Path, run: dict[str, Any], entry: Any
) -> LoadedSnapshot:
    if not isinstance(entry, dict):
        raise ContextError("run snapshot entry is invalid")
    try:
        object_kind = entry["object_kind"]
        source_kind = entry["source_kind"]
        source_id = entry["source_id"]
        relative = _archive_relative_path(entry["path"])
    except KeyError, ArchiveError:
        raise ContextError("run snapshot entry is invalid") from None
    if not all(
        isinstance(value, str) and value
        for value in (source_kind, object_kind, source_id)
    ):
        raise ContextError("snapshot identity is inconsistent")
    if source_kind != run["source"][
        "kind"
    ] or object_kind not in _SUPPORTED_OBJECT_KINDS.get(source_kind, set()):
        raise ContextError("snapshot identity is inconsistent")
    expected_relative = PurePosixPath(
        "snapshots", object_kind, encode_path_id(source_id)
    )
    if relative != expected_relative:
        raise ContextError("snapshot path identity is inconsistent")
    root: Path = run_root.joinpath(*relative.parts)
    _ensure_within(root, run_root / "snapshots")
    if not root.is_dir() or root.is_symlink():
        raise ContextError("snapshot directory is not regular")
    manifest = _json_file(root / "snapshot.json", "snapshot manifest")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ContextError("unsupported snapshot schema")
    if manifest.get("source_kind") != source_kind:
        raise ContextError("snapshot source kind is inconsistent")
    if manifest.get("object_kind") != object_kind:
        raise ContextError("snapshot object kind is inconsistent")
    if manifest.get("source_id") != source_id:
        raise ContextError("snapshot identity is inconsistent")
    observation_window = manifest.get("observation_window")
    if not isinstance(observation_window, dict):
        raise ContextError("snapshot observation window is invalid")
    if not isinstance(observation_window.get("from"), str) or not isinstance(
        observation_window.get("to"), str
    ):
        raise ContextError("snapshot observation window is invalid")
    try:
        observation_start = ContextRequest._endpoint(observation_window["from"])
        observation_end = ContextRequest._endpoint(observation_window["to"])
    except ContextError:
        raise ContextError("snapshot observation window is invalid") from None
    if observation_start >= observation_end:
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
        _ensure_within(file_path, root)
        if not _is_regular_file(file_path):
            raise ContextError("declared evidence file is not regular")
        try:
            content = file_path.read_bytes()
        except OSError:
            raise ContextError("declared evidence file is unreadable") from None
        if path.suffix.lower() == ".json":
            try:
                json.loads(content)
            except UnicodeError, json.JSONDecodeError:
                raise ContextError("declared JSON evidence is invalid") from None
        evidence[path.as_posix()] = content
    actual: set[str] = set()
    for child in root.rglob("*"):
        _ensure_within(child, root)
        if child == root / "snapshot.json" or child.is_dir():
            continue
        if not _is_regular_file(child):
            raise ContextError("snapshot contains a non-regular file")
        actual.add(child.relative_to(root).as_posix())
    if actual != set(evidence):
        raise ContextError("snapshot evidence does not match its manifest")
    declared_directories: set[str] = set()
    for evidence_path in evidence:
        parent = PurePosixPath(evidence_path).parent
        while parent != PurePosixPath("."):
            declared_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {
        child.relative_to(root).as_posix()
        for child in root.rglob("*")
        if child.is_dir()
    }
    if actual_directories != declared_directories:
        raise ContextError("snapshot directories do not match its manifest")
    return LoadedSnapshot(run, manifest, evidence, root)


def load_archive(root: str | Path) -> tuple[LoadedRun, ...]:  # noqa: PLR0915
    """Load and validate every published run without consulting external sources."""
    archive = Path(root).absolute()
    runs_root = archive / "runs"
    if not runs_root.exists():
        return ()
    if not runs_root.is_dir() or runs_root.is_symlink():
        raise ContextError("published runs root is not a regular directory")
    loaded: list[LoadedRun] = []
    for run_root in sorted(runs_root.iterdir(), key=lambda path: path.name):
        _ensure_within(run_root, runs_root)
        if not run_root.is_dir() or run_root.is_symlink():
            raise ContextError("published run is not a regular directory")
        run = _json_file(run_root / "run.json", "run manifest")
        if {path.name for path in run_root.iterdir()} != {"run.json", "snapshots"}:
            raise ContextError("published run contains unregistered entries")
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
        if not isinstance(run.get("coverage"), dict):
            raise ContextError("run coverage is invalid")
        started_at = run.get("started_at")
        completed_at = run.get("completed_at")
        if not isinstance(started_at, str) or not isinstance(completed_at, str):
            raise ContextError("run timestamps are invalid")
        try:
            started = ContextRequest._endpoint(started_at)
            completed = ContextRequest._endpoint(completed_at)
        except ContextError:
            raise ContextError("run timestamps are invalid") from None
        if started > completed:
            raise ContextError("run timestamps are invalid")
        collector = run.get("collector")
        if not isinstance(collector, dict):
            raise ContextError("run collector is invalid")
        if not isinstance(collector.get("version"), str) or not collector["version"]:
            raise ContextError("run collector is invalid")
        if not isinstance(collector.get("effective_options"), dict):
            raise ContextError("run collector is invalid")
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
        expected_object_kinds: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ContextError("run snapshot entry is invalid")
            try:
                snapshot_path = _archive_relative_path(entry["path"])
            except ArchiveError:
                raise ContextError("run snapshot entry is invalid") from None
            if (
                len(snapshot_path.parts) != _SNAPSHOT_PATH_PARTS
                or snapshot_path.parts[0] != "snapshots"
            ):
                raise ContextError("run snapshot entry is invalid")
            expected_object_kinds.add(snapshot_path.parts[1])
        actual_object_kinds = {path.name for path in snapshots_root.iterdir()}
        if actual_object_kinds != expected_object_kinds:
            raise ContextError("snapshot object directories do not match its manifest")
        for object_root in snapshots_root.iterdir():
            if not object_root.is_dir() or object_root.is_symlink():
                raise ContextError("snapshot object directory is invalid")
            expected_snapshots = {
                PurePosixPath(entry["path"]).parts[2]
                for entry in entries
                if entry["path"].startswith(f"snapshots/{object_root.name}/")
            }
            actual_snapshots = {path.name for path in object_root.iterdir()}
            if actual_snapshots != expected_snapshots:
                raise ContextError("snapshot directories do not match its manifest")
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
            if (
                key in _NUMERIC_TIMESTAMP_KEYS
                and isinstance(child, (int, float))
                and not isinstance(child, bool)
            ):
                try:
                    found.append(datetime.fromtimestamp(child / 1000, tz=UTC))
                except OverflowError, OSError, ValueError:
                    continue
            found.extend(_timestamps(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_timestamps(child))
    return found


def _snapshot_records(snapshot: LoadedSnapshot) -> list[_TemporalRecord]:
    records: list[_TemporalRecord] = []
    for name, content in snapshot.evidence.items():
        if not name.endswith(".json"):
            continue
        try:
            value = json.loads(content)
            if snapshot.manifest["source_kind"] == "opencode":
                records.extend(_opencode_records(value))
            elif isinstance(value, (dict, list)):
                records.extend(
                    _TemporalRecord(timestamp, None) for timestamp in _timestamps(value)
                )
        except UnicodeError, json.JSONDecodeError:
            continue
    return records


def _record_intersects(record: _TemporalRecord, request: ContextRequest) -> bool:
    if record.kind == "context":
        return False
    if record.end is None:
        return request.start <= record.start < request.end
    if record.start == record.end:
        return request.start <= record.start < request.end
    return record.start < request.end and record.end > request.start


def _snapshot_semantic_time(
    snapshot: LoadedSnapshot,
    records: list[_TemporalRecord],
    request: ContextRequest,
) -> datetime:
    if records:
        selected = [record for record in records if _record_intersects(record, request)]
        if selected:
            return min(record.start for record in selected)
        return min(record.start for record in records)
    window = snapshot.manifest["observation_window"]
    try:
        return ContextRequest._endpoint(window["from"])
    except ContextError:
        return datetime.min.replace(tzinfo=UTC)


def _logical_key(snapshot: LoadedSnapshot) -> tuple[str, str, str, str]:
    return (
        snapshot.manifest["source_kind"],
        snapshot.run["source"]["scope_id"],
        snapshot.manifest["object_kind"],
        snapshot.manifest["source_id"],
    )


def _item_path(key: tuple[str, str, str, str]) -> str:
    return f"{key[0]}/{encode_path_id(key[1])}/{key[2]}/{encode_path_id(key[3])}"


def _make_context_item(
    key: tuple[str, str, str, str],
    snapshots: list[LoadedSnapshot],
    record_map: dict[int, list[_TemporalRecord]],
    request: ContextRequest,
    extra_reasons: tuple[str, ...] = (),
) -> ContextItem:
    all_records = [
        record for snapshot in snapshots for record in record_map[id(snapshot)]
    ]
    selected_snapshot_ids = {
        id(snapshot)
        for snapshot in snapshots
        if any(
            _record_intersects(record, request) for record in record_map[id(snapshot)]
        )
    }
    selected = any(_record_intersects(record, request) for record in all_records)
    roles: list[str] = []
    if selected:
        roles.append("in_range_record")
    if any(record.start < request.start for record in all_records):
        roles.append("prior_background")
    if any(record.start >= request.end for record in all_records):
        roles.append("later_development")
    if any(_has_observed_state(snapshot) for snapshot in snapshots):
        roles.append("observed_state")
    reasons = list(extra_reasons)
    if selected:
        reasons.insert(0, "source_record_in_range")
    if "prior_background" in roles:
        reasons.append("prior_background")
    if "later_development" in roles:
        reasons.append("later_evidence")
    if "observed_state" in roles:
        reasons.append("observed_state")
    if any(
        id(snapshot) not in selected_snapshot_ids for snapshot in snapshots
    ) and not any(
        reason in reasons
        for reason in ("prior_background", "later_evidence", "observed_state")
    ):
        reasons.append("object_context")
    return ContextItem(
        tuple(
            sorted(
                snapshots,
                key=lambda snapshot: (
                    _snapshot_semantic_time(
                        snapshot, record_map[id(snapshot)], request
                    ),
                    snapshot.run["run_id"],
                ),
            )
        ),
        tuple(dict.fromkeys(reasons)),
        tuple(roles),
        _item_path(key),
    )


def _snapshot_relationships(
    snapshot: LoadedSnapshot,
) -> list[tuple[str, str, str]]:
    relationships: list[tuple[str, str, str]] = []
    for name, content in snapshot.evidence.items():
        if name.endswith(".json"):
            relationships.extend(
                (kind, target, name)
                for kind, target in _relationship_references(json.loads(content))
            )
    return relationships


def _group_context_snapshots(
    runs: tuple[LoadedRun, ...],
) -> tuple[
    dict[tuple[str, str, str, str], list[LoadedSnapshot]],
    dict[int, list[_TemporalRecord]],
]:
    grouped: dict[tuple[str, str, str, str], list[LoadedSnapshot]] = {}
    record_map: dict[int, list[_TemporalRecord]] = {}
    for run in runs:
        for snapshot in run.snapshots:
            records = _snapshot_records(snapshot)
            record_map[id(snapshot)] = records
            key = _logical_key(snapshot)
            grouped.setdefault(key, []).append(snapshot)
    return grouped, record_map


def _select_context_items(
    grouped: dict[tuple[str, str, str, str], list[LoadedSnapshot]],
    record_map: dict[int, list[_TemporalRecord]],
    request: ContextRequest,
) -> tuple[
    dict[tuple[str, str, str, str], ContextItem],
    set[tuple[str, str, str, str]],
    list[dict[str, str]],
]:
    items_by_key: dict[tuple[str, str, str, str], ContextItem] = {}
    selected_keys: set[tuple[str, str, str, str]] = set()
    gaps: list[dict[str, str]] = []
    for key, snapshots in grouped.items():
        all_records = [
            record for snapshot in snapshots for record in record_map[id(snapshot)]
        ]
        selected_records = [
            record for record in all_records if _record_intersects(record, request)
        ]
        if not selected_records:
            continue
        items_by_key[key] = _make_context_item(key, snapshots, record_map, request)
        selected_keys.add(key)
        gaps.extend(
            {
                "kind": "unknown_completion",
                "source_kind": key[0],
                "object_kind": key[2],
                "source_id": key[3],
            }
            for record in selected_records
            if record.kind == "execution" and record.end is None
        )
    return items_by_key, selected_keys, gaps


def _expand_session_relations(
    grouped: dict[tuple[str, str, str, str], list[LoadedSnapshot]],
    record_map: dict[int, list[_TemporalRecord]],
    items_by_key: dict[tuple[str, str, str, str], ContextItem],
    selected_keys: set[tuple[str, str, str, str]],
    request: ContextRequest,
) -> tuple[
    list[tuple[str, tuple[str, str, str, str], tuple[str, str, str, str]]],
    list[dict[str, str]],
]:
    parent_child_relation_keys: list[
        tuple[str, tuple[str, str, str, str], tuple[str, str, str, str]]
    ] = []
    gaps: list[dict[str, str]] = []
    relation_keys_seen: set[
        tuple[str, tuple[str, str, str, str], tuple[str, str, str, str]]
    ] = set()
    parent_queue = sorted(selected_keys)
    processed_parent_keys: set[tuple[str, str, str, str]] = set()
    while parent_queue:
        source_key = parent_queue.pop(0)
        if source_key[0] != "opencode" or source_key[2] != "session":
            processed_parent_keys.add(source_key)
            continue
        source_item = items_by_key[source_key]
        source_scope = source_key[1]
        for relationship, target_id, _ in (
            relationship
            for snapshot in source_item.snapshots
            for relationship in _snapshot_relationships(snapshot)
        ):
            target_key = ("opencode", source_scope, "session", target_id)
            if relationship == "parent_session":
                relation_kind = "parent_session"
                supporting_reason = "parent_context"
            elif relationship == "child_task" and source_key in selected_keys:
                relation_kind = "child_task"
                supporting_reason = "child_context"
            else:
                continue
            if target_key not in grouped:
                gaps.append(
                    {
                        "kind": (
                            "missing_parent_reference"
                            if relationship == "parent_session"
                            else "missing_child_reference"
                        ),
                        "source_kind": source_key[0],
                        "source_scope_id": source_scope,
                        "object_kind": source_key[2],
                        "source_id": source_key[3],
                        "target_id": target_id,
                    }
                )
                continue
            if target_key not in items_by_key:
                items_by_key[target_key] = _make_context_item(
                    target_key,
                    grouped[target_key],
                    record_map,
                    request,
                    (supporting_reason,),
                )
            else:
                target_item = items_by_key[target_key]
                if supporting_reason not in target_item.reasons:
                    items_by_key[target_key] = replace(
                        target_item,
                        reasons=(*target_item.reasons, supporting_reason),
                    )
            relation_key = (relation_kind, source_key, target_key)
            if relation_key not in relation_keys_seen:
                relation_keys_seen.add(relation_key)
                parent_child_relation_keys.append(relation_key)
            if (
                relationship == "parent_session"
                and target_key not in processed_parent_keys
            ):
                parent_queue.append(target_key)
        processed_parent_keys.add(source_key)
    return parent_child_relation_keys, gaps


def extract_context(
    request: ContextRequest, runs: tuple[LoadedRun, ...]
) -> ContextExtractionResult:
    """Build a disposable result whose items directly reference loaded snapshots."""
    grouped, record_map = _group_context_snapshots(runs)
    items_by_key, selected_keys, gaps = _select_context_items(
        grouped, record_map, request
    )
    parent_child_relation_keys, relation_gaps = _expand_session_relations(
        grouped, record_map, items_by_key, selected_keys, request
    )
    gaps.extend(relation_gaps)
    items = sorted(
        items_by_key.values(), key=lambda item: (item.path, item.source.run["run_id"])
    )
    for item in items:
        for snapshot in item.snapshots:
            gaps.extend(
                {
                    **gap,
                    "source_kind": snapshot.manifest["source_kind"],
                    "object_kind": snapshot.manifest["object_kind"],
                    "source_id": snapshot.manifest["source_id"],
                }
                for gap in _snapshot_semantic_gaps(snapshot)
            )
    relations, unresolved, reference_gaps, explicit_relations = _reference_data(
        tuple(items), runs
    )
    gaps.extend(reference_gaps)
    native_relations = _native_relation_data(tuple(items))
    direct_relations: list[ContextRelation] = list(explicit_relations)
    for relation_kind, source_key, target_key in parent_child_relation_keys:
        source_item = items_by_key[source_key]
        target_item = items_by_key[target_key]
        direct_relations.append(
            ContextRelation(relation_kind, source_item, target_item)
        )
        relations += (
            {
                "kind": relation_kind,
                "from": source_item.path,
                "target": target_item.path,
            },
        )
    relations = (*native_relations, *relations)
    return ContextExtractionResult(
        request,
        runs,
        tuple(items),
        tuple(gaps),
        relations,
        unresolved,
        tuple(direct_relations),
    )


def _has_observed_state(snapshot: LoadedSnapshot) -> bool:
    return any(
        marker in content.decode("utf-8", errors="ignore")
        for content in snapshot.evidence.values()
        for marker in ('"isResolved"', '"isOutdated"', '"status"')
    )


def _json_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(value, path)]
    if isinstance(value, dict):
        return [
            occurrence
            for key, child in value.items()
            for occurrence in _json_strings(child, f"{path}.{key}")
        ]
    if isinstance(value, list):
        return [
            occurrence
            for index, child in enumerate(value)
            for occurrence in _json_strings(child, f"{path}[{index}]")
        ]
    return []


def _snapshot_strings(snapshot: LoadedSnapshot) -> list[tuple[str, str, str]]:
    strings: list[tuple[str, str, str]] = []
    for name, content in snapshot.evidence.items():
        if not name.endswith(".json"):
            continue
        try:
            strings.extend(
                (name, path, text) for text, path in _json_strings(json.loads(content))
            )
        except UnicodeError, json.JSONDecodeError:
            continue
    return strings


def _github_reference_key(url: str) -> tuple[str, str, str, str] | None:
    match = _GITHUB_REFERENCE.fullmatch(url)
    if match is None:
        return None
    return (
        match.group(1).lower(),
        match.group(2).lower(),
        "pull-request" if match.group(3).lower() == "pull" else "issue",
        match.group(4),
    )


def _github_item_keys(snapshot: LoadedSnapshot) -> set[tuple[str, str, str, str]]:
    keys: set[tuple[str, str, str, str]] = set()
    primary_name = (
        "pull-request.json"
        if snapshot.manifest["object_kind"] == "pull-request"
        else "issue.json"
    )
    payload = (
        json.loads(snapshot.evidence[primary_name])
        if primary_name in snapshot.evidence
        else None
    )
    if not isinstance(payload, dict):
        return keys
    number = payload.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        number = None
    identity_urls = [
        payload.get("html_url"),
        payload.get("url"),
        payload.get("repository_url"),
    ]
    for identity_url in identity_urls:
        if not isinstance(identity_url, str):
            continue
        key = _github_reference_key(identity_url)
        if key is None:
            api_match = _GITHUB_API_ITEM.fullmatch(identity_url)
            if api_match is not None and number is not None:
                key = (
                    api_match.group(1).lower(),
                    api_match.group(2).lower(),
                    "pull-request"
                    if api_match.group(3).lower() == "pulls"
                    else "issue",
                    str(number),
                )
        if key is not None and key[2] == snapshot.manifest["object_kind"]:
            keys.add(key)
    return keys


def _reference_data(
    items: tuple[ContextItem, ...], runs: tuple[LoadedRun, ...]
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, str], ...],
    list[dict[str, str]],
    tuple[ContextRelation, ...],
]:
    targets: dict[
        tuple[str, str, str, str],
        dict[tuple[str, str, str, str], tuple[dict[str, str], LoadedSnapshot]],
    ] = {}
    for run in runs:
        for snapshot in run.snapshots:
            if snapshot.manifest["source_kind"] != "github":
                continue
            for key in _github_item_keys(snapshot):
                target = {
                    "source_kind": "github",
                    "source_scope_id": run.manifest["source"]["scope_id"],
                    "object_kind": key[2],
                    "source_id": snapshot.manifest["source_id"],
                    "path": (
                        f"github/{encode_path_id(run.manifest['source']['scope_id'])}/"
                        f"{key[2]}/{encode_path_id(snapshot.manifest['source_id'])}"
                    ),
                }
                logical_key = (
                    target["source_kind"],
                    target["source_scope_id"],
                    target["object_kind"],
                    target["source_id"],
                )
                targets.setdefault(key, {})[logical_key] = (target, snapshot)
    relations: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    gaps: list[dict[str, str]] = []
    direct_relations: list[ContextRelation] = []
    seen_occurrences: set[tuple[str, str, str, int, str]] = set()
    for item in items:
        if item.source.manifest["source_kind"] != "opencode":
            continue
        for snapshot in item.snapshots:
            for evidence_path, json_path, text in _snapshot_strings(snapshot):
                match = _GITHUB_REFERENCE.search(text)
                while match is not None:
                    url = match.group(0)
                    reference_key = _github_reference_key(url)
                    if reference_key is None:
                        break
                    occurrence_key = (
                        item.path,
                        evidence_path,
                        json_path,
                        match.start(),
                        url,
                    )
                    if occurrence_key in seen_occurrences:
                        match = _GITHUB_REFERENCE.search(text, match.end())
                        continue
                    seen_occurrences.add(occurrence_key)
                    matches = sorted(
                        targets.get(reference_key, {}).values(),
                        key=lambda target: (
                            target[0]["source_scope_id"],
                            target[0]["source_id"],
                        ),
                    )
                    if matches:
                        for target, target_snapshot in matches:
                            occurrence = {
                                "evidence_path": evidence_path,
                                "json_path": json_path,
                                "offset": match.start(),
                                "length": len(url),
                            }
                            relations.append(
                                {
                                    "kind": "explicit_reference",
                                    "from": item.path,
                                    "target": target,
                                    "occurrence": occurrence,
                                }
                            )
                            direct_relations.append(
                                ContextRelation(
                                    "explicit_reference",
                                    item,
                                    target_snapshot,
                                    occurrence,
                                )
                            )
                    else:
                        unresolved.append(
                            {
                                "from": item.path,
                                "url": url,
                                "evidence_path": evidence_path,
                                "json_path": json_path,
                                "offset": str(match.start()),
                            }
                        )
                        gaps.append(
                            {
                                "kind": "unresolved_explicit_reference",
                                "source_path": item.path,
                                "url": url,
                                "evidence_path": evidence_path,
                                "json_path": json_path,
                                "offset": str(match.start()),
                            }
                        )
                    match = _GITHUB_REFERENCE.search(text, match.end())
    return tuple(relations), tuple(unresolved), gaps, tuple(direct_relations)


def _github_evidence_category(  # noqa: PLR0911
    object_kind: str, name: str
) -> str:
    if name == "issue.json":
        return "issue payload"
    if name == "pull-request.json":
        return "pull request payload"
    if name.startswith("comments."):
        return "ordinary comments"
    if name.startswith("timeline."):
        return "timeline entries"
    if name.startswith("reviews."):
        return "submitted reviews"
    if name.startswith("review-comments."):
        return "inline review comments"
    if name.startswith("review-threads.") or name.startswith("review-thread-comments."):
        return "review threads"
    if name == "pull-request.diff":
        return "aggregate pull request diff"
    return f"{object_kind} source-native evidence"


def _contains_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return bool(keys & value.keys()) or any(
            _contains_key(child, keys) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(child, keys) for child in value)
    return False


def _contains_flag(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        if any(key in keys and child is True for key, child in value.items()):
            return True
        return any(_contains_flag(child, keys) for child in value.values())
    if isinstance(value, list):
        return any(_contains_flag(child, keys) for child in value)
    return False


def _unknown_opencode_types(value: Any) -> set[str]:
    known = {
        "session",
        "session-export",
        "message",
        "user",
        "assistant",
        "system",
        "developer",
        "text",
        "reasoning",
        "tool",
        "tool-call",
        "tool-result",
        "tool_result",
        "step-start",
        "step-finish",
        "patch",
        "file",
        "compaction",
        "compaction-summary",
        "synthetic",
        "synthetic-continuation",
        "continuation",
    }
    found: set[str] = set()
    if isinstance(value, dict):
        record_type = value.get("type")
        if isinstance(record_type, str) and record_type not in known:
            found.add(record_type)
        for child in value.values():
            found.update(_unknown_opencode_types(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_unknown_opencode_types(child))
    return found


def _relationship_references(
    value: Any, context: str = "root"
) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    if isinstance(value, dict):
        record_type = value.get("type")
        if record_type in {"compaction", "compaction-summary"}:
            context = "compaction"
        elif context == "root" and (
            "tool" in value or "callID" in value or record_type in {"tool", "tool-call"}
        ):
            context = "task"
        for key, child in value.items():
            normalized = key.replace("_", "").lower()
            if normalized in {"parentmessageid", "messageparentid"}:
                relation = "message_parent"
            elif normalized in {
                "compactiontailid",
                "tailid",
                "continuationid",
            } or (context == "compaction" and normalized == "parentid"):
                relation = "compaction_tail"
            elif normalized in {"childid", "childsessionid", "taskchildid", "children"}:
                relation = "child_task"
            elif normalized == "parentsessionid":
                relation = "parent_session"
            elif normalized == "parentid":
                relation = (
                    "message_parent" if context == "message" else "parent_session"
                )
            else:
                relation = None
            if relation is not None:
                if isinstance(child, str):
                    references.append((relation, child))
                elif isinstance(child, list):
                    references.extend(
                        (relation, target)
                        for target in child
                        if isinstance(target, str)
                    )
            child_context = context
            if normalized in {"messages", "message", "messagehistory"}:
                child_context = "message"
            references.extend(_relationship_references(child, child_context))
    elif isinstance(value, list):
        for child in value:
            references.extend(_relationship_references(child, context))
    return references


def _native_relation_data(
    items: tuple[ContextItem, ...],
) -> tuple[dict[str, Any], ...]:
    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        for snapshot in item.snapshots:
            for kind, target_id, evidence_path in _snapshot_relationships(snapshot):
                if kind in {"parent_session", "child_task"}:
                    continue
                relation_key = (item.path, kind, target_id, evidence_path)
                if relation_key in seen:
                    continue
                seen.add(relation_key)
                relations.append(
                    {
                        "kind": kind,
                        "from": item.path,
                        "target_id": target_id,
                        "source_scope_id": snapshot.run["source"]["scope_id"],
                        "evidence_path": evidence_path,
                    }
                )
    return tuple(relations)


def _snapshot_semantic_gaps(snapshot: LoadedSnapshot) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    for name, content in snapshot.evidence.items():
        if not name.endswith(".json"):
            continue
        value = json.loads(content)
        if _contains_flag(value, {"truncated", "isTruncated", "incomplete_results"}):
            gaps.append({"kind": "source_truncated", "evidence_path": name})
        if snapshot.manifest["source_kind"] == "opencode":
            gaps.extend(
                {
                    "kind": "unknown_part_type",
                    "evidence_path": name,
                    "part_type": part_type,
                }
                for part_type in sorted(_unknown_opencode_types(value))
            )
    return gaps


def _opencode_evidence_categories(name: str, content: bytes) -> tuple[str, ...]:
    categories: set[str] = {"session export"} if name == "session.json" else set()
    text = content.decode("utf-8", errors="ignore")
    try:
        value = json.loads(content)
    except UnicodeError, json.JSONDecodeError:
        return ("source-native evidence",)
    if _contains_key(value, {"messages"}):
        categories.add("messages")
    if _contains_key(value, {"parts"}):
        categories.add("message parts")
    if _contains_key(value, {"tool", "callID"}) or (
        _contains_key(value, {"type"})
        and any(marker in text for marker in ('"tool"', '"tool-call"', '"callID"'))
    ):
        categories.add("tool records")
    if _contains_key(value, {"status", "error"}):
        categories.add("tool status/results")
    if _contains_key(value, {"truncated", "isTruncated"}):
        categories.add("truncation markers")
    if _contains_key(value, {"compaction", "compactionSummary"}) or any(
        marker in text for marker in ('"type":"compaction"', '"type": "compaction"')
    ):
        categories.add("compaction summaries")
    if _contains_key(value, {"synthetic", "isSynthetic"}) or any(
        marker in text for marker in ('"type":"synthetic"', '"type": "synthetic"')
    ):
        categories.add("synthetic continuations")
    if _unknown_opencode_types(value):
        categories.add("unknown part types")
    if _contains_key(
        value, {"parentID", "parent_id", "childID", "child_id", "children"}
    ):
        categories.add("parent/child relationships")
    return tuple(sorted(categories or {"source-native evidence"}))


def _relation_index_line(relation: dict[str, Any]) -> str:
    if "occurrence" not in relation:
        target = relation.get("target", relation.get("target_id", "unknown"))
        return f"- {relation['kind']} from `{relation['from']}` to `{target}`"
    occurrence = relation["occurrence"]
    return (
        f"- Resolved from `{relation['from']}` "
        f"`{occurrence['evidence_path']}` "
        f"at `{occurrence['json_path']}:{occurrence['offset']}` "
        f"to `{relation['target']['path']}`"
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


def _render_item(staging: Path, item: ContextItem) -> dict[str, Any]:
    item_root = staging.joinpath(*PurePosixPath(item.path).parts)
    item_root.mkdir(parents=True, exist_ok=True)
    provenance: list[dict[str, Any]] = []
    for observation_index, snapshot in enumerate(item.snapshots, start=1):
        run = snapshot.run
        run_id = run["run_id"]
        observation_name = f"{observation_index:03d}-{run_id}"
        observation_root = item_root / "observations" / observation_name
        for name, content in sorted(snapshot.evidence.items()):
            destination = observation_root.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        provenance.append(
            {
                "run_id": run_id,
                "source": run["source"],
                "collection_range": run["collection_range"],
                "started_at": run["started_at"],
                "completed_at": run["completed_at"],
                "collector": run["collector"],
                "coverage": run.get("coverage", {}),
                "run_manifest": run,
                "snapshot": snapshot.manifest,
                "output_path": f"{item.path}/observations/{observation_name}",
            }
        )
    source_kind = item.source.manifest["source_kind"]
    object_kind = item.source.manifest["object_kind"]
    view_name = f"{source_kind}.md"
    view_title = (
        "GitHub Issue"
        if source_kind == "github" and object_kind == "issue"
        else "GitHub Pull Request"
        if source_kind == "github"
        else "OpenCode Session"
    )
    view_lines = [
        f"# {view_title}",
        "",
        f"- Object kind: `{item.source.manifest['object_kind']}`",
        f"- Source ID: `{item.source.manifest['source_id']}`",
        f"- Scope ID: `{item.source.run['source']['scope_id']}`",
        f"- Inclusion reasons: {', '.join(item.reasons)}",
        f"- Temporal roles: {', '.join(item.temporal_roles)}",
        "",
        "## Source-native Evidence",
        "",
    ]
    for observation_index, snapshot in enumerate(item.snapshots, start=1):
        observation_name = f"{observation_index:03d}-{snapshot.run['run_id']}"
        view_lines.extend(
            f"- [{name}](observations/{observation_name}/{name}) — "
            + (
                _github_evidence_category(object_kind, name)
                if source_kind == "github"
                else ", ".join(
                    _opencode_evidence_categories(name, snapshot.evidence[name])
                )
            )
            for name in sorted(snapshot.evidence)
        )
    (item_root / view_name).write_text("\n".join(view_lines) + "\n", encoding="utf-8")
    view_path = f"{item.path}/{view_name}"
    return {
        "source_kind": source_kind,
        "source_scope_id": item.source.run["source"]["scope_id"],
        "object_kind": object_kind,
        "source_id": item.source.manifest["source_id"],
        "inclusion_reasons": list(item.reasons),
        "temporal_roles": list(item.temporal_roles),
        "path": item.path,
        "view_path": view_path,
        "provenance": provenance,
    }


def _render_index(
    staging: Path,
    result: ContextExtractionResult,
    item_manifests: list[dict[str, Any]],
) -> None:
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
            f"- `{item['path']}/` [{item['source_kind']} view]({item['view_path']}) "
            f"reasons={','.join(item['inclusion_reasons'])} "
            f"roles={','.join(item['temporal_roles'])}"
            for item in item_manifests
        )
    lines.extend(["", "## Explicit References", ""])
    if not result.relations and not result.unresolved_references:
        lines.append("No Explicit References recorded.")
    else:
        lines.extend(_relation_index_line(relation) for relation in result.relations)
        lines.extend(
            f"- Unresolved `{reference['url']}` from `{reference['from']}` "
            f"in `{reference['evidence_path']}` "
            f"at `{reference['json_path']}:{reference['offset']}`"
            for reference in result.unresolved_references
        )
    lines.extend(
        ["", "## Gaps", "", "No gaps recorded." if not result.gaps else "", ""]
    )
    if result.gaps:
        lines[-1:-1] = [
            f"- {json.dumps(gap, ensure_ascii=False, sort_keys=True)}"
            for gap in result.gaps
        ]
    (staging / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _render_manifest(
    staging: Path,
    result: ContextExtractionResult,
    item_manifests: list[dict[str, Any]],
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
            "source_items": item_manifests,
            "relations": list(result.relations),
            "unresolved_references": list(result.unresolved_references),
            "gaps": list(result.gaps),
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
        item_manifests = [_render_item(staging, item) for item in result.items]
        _render_index(staging, result, item_manifests)
        _render_manifest(staging, result, item_manifests)
        staging.rename(target)
        return target
    except OSError, TypeError, ValueError:
        raise ContextError("context output publication failed") from None
    finally:
        _cleanup_staging(staging)


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    runs = load_archive(archive)
    return render_context(extract_context(request, runs), output)
