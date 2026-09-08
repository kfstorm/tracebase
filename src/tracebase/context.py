"""Offline Context Output extraction from the published Raw Archive."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class _TemporalRecord:
    start: datetime


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
    return bool({"role", "callID", "tool", "body", "text"} & value.keys()) or (
        isinstance(value.get("parts"), list) and bool(value["parts"])
    )


def _opencode_records(value: Any) -> list[_TemporalRecord]:
    records: list[_TemporalRecord] = []
    if isinstance(value, dict):
        fields = _record_fields(value)
        if fields and _is_actual_opencode_record(value):
            records.extend(_TemporalRecord(timestamp) for _, timestamp in fields)
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


def _snapshot_entry_path(entry: Any) -> PurePosixPath:
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        raise ContextError("run snapshot entry is invalid")
    try:
        relative = _archive_relative_path(entry["path"])
    except ArchiveError:
        raise ContextError("run snapshot entry is invalid") from None
    if len(relative.parts) != _SNAPSHOT_PATH_PARTS or relative.parts[0] != "snapshots":
        raise ContextError("run snapshot entry is invalid")
    return relative


def _snapshot_identity(
    run_root: Path, run: dict[str, Any], entry: Any
) -> tuple[str, str, str, Path]:
    if not isinstance(entry, dict):
        raise ContextError("run snapshot entry is invalid")
    try:
        object_kind = entry["object_kind"]
        source_kind = entry["source_kind"]
        source_id = entry["source_id"]
    except KeyError:
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
    relative = _snapshot_entry_path(entry)
    expected_relative = PurePosixPath(
        "snapshots", object_kind, encode_path_id(source_id)
    )
    if relative != expected_relative:
        raise ContextError("snapshot path identity is inconsistent")
    root = run_root.joinpath(*relative.parts)
    _ensure_within(root, run_root / "snapshots")
    if not root.is_dir() or root.is_symlink():
        raise ContextError("snapshot directory is not regular")
    return source_kind, object_kind, source_id, root


def _validate_snapshot_manifest(
    root: Path, source_kind: str, object_kind: str, source_id: str
) -> dict[str, Any]:
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
    if (
        not isinstance(observation_window, dict)
        or not isinstance(observation_window.get("from"), str)
        or not isinstance(observation_window.get("to"), str)
    ):
        raise ContextError("snapshot observation window is invalid")
    try:
        observation_start = ContextRequest._endpoint(observation_window["from"])
        observation_end = ContextRequest._endpoint(observation_window["to"])
    except ContextError:
        raise ContextError("snapshot observation window is invalid") from None
    if observation_start >= observation_end:
        raise ContextError("snapshot observation window is invalid")
    return manifest


def _load_declared_evidence(root: Path, manifest: dict[str, Any]) -> dict[str, bytes]:
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
        file_path = root.joinpath(*path.parts)
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
    return evidence


def _validate_evidence_tree(root: Path, evidence: dict[str, bytes]) -> None:
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


def _native_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except UnicodeError, json.JSONDecodeError:
        raise ContextError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise ContextError(f"{label} is invalid")
    return value


def _validate_native_schema(
    source_kind: str, object_kind: str, evidence: dict[str, bytes]
) -> None:
    required_evidence: tuple[str, ...] = ("issue.json",)
    if source_kind == "github" and object_kind == "pull-request":
        required_evidence = ("issue.json", "pull-request.json")
    elif source_kind == "opencode":
        required_evidence = ("session.json",)
    if any(name not in evidence for name in required_evidence):
        raise ContextError("unsupported source schema")
    if source_kind == "opencode":
        payload = _native_json_object(evidence["session.json"], "session evidence")
        if (
            not payload
            or not {"messages", "parts", "role", "text", "body", "payload"}
            & payload.keys()
        ):
            raise ContextError("unsupported source schema")
        return
    issue = _native_json_object(evidence["issue.json"], "GitHub issue evidence")
    if not isinstance(issue.get("node_id"), str) or not issue["node_id"]:
        raise ContextError("unsupported source schema")
    if ("pull_request" in issue) != (object_kind == "pull-request"):
        raise ContextError("unsupported source schema")
    if object_kind == "pull-request":
        pull_request = _native_json_object(
            evidence["pull-request.json"], "GitHub pull request evidence"
        )
        if (
            not isinstance(pull_request.get("node_id"), str)
            or not pull_request["node_id"]
        ):
            raise ContextError("unsupported source schema")


def _load_snapshot(run_root: Path, run: dict[str, Any], entry: Any) -> LoadedSnapshot:
    source_kind, object_kind, source_id, root = _snapshot_identity(run_root, run, entry)
    manifest = _validate_snapshot_manifest(root, source_kind, object_kind, source_id)
    evidence = _load_declared_evidence(root, manifest)
    _validate_evidence_tree(root, evidence)
    _validate_native_schema(source_kind, object_kind, evidence)
    return LoadedSnapshot(run, manifest, evidence, root)


def _validate_run_manifest(run_root: Path) -> dict[str, Any]:
    run = _json_file(run_root / "run.json", "run manifest")
    if (
        run.get("format_version") != FORMAT_VERSION
        or run.get("run_id") != run_root.name
    ):
        raise ContextError("run manifest identity is invalid")
    source = run.get("source")
    if not isinstance(source, dict) or source.get("kind") not in {"github", "opencode"}:
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
    return run


def _validate_snapshot_entries(
    run: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    entries = run.get("snapshots")
    if not isinstance(entries, list):
        raise ContextError("run snapshots are invalid")
    listed_paths: set[str] = set()
    expected_object_kinds: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            if entry["path"] in listed_paths:
                raise ContextError("run snapshot paths are not unique")
            listed_paths.add(entry["path"])
        snapshot_path = _snapshot_entry_path(entry)
        expected_object_kinds.add(snapshot_path.parts[1])
    return entries, expected_object_kinds


def _validate_published_layout(
    run_root: Path, entries: list[dict[str, Any]], expected_object_kinds: set[str]
) -> None:
    if {path.name for path in run_root.iterdir()} != {"run.json", "snapshots"}:
        raise ContextError("published run contains unregistered entries")
    snapshots_root = run_root / "snapshots"
    if not snapshots_root.is_dir() or snapshots_root.is_symlink():
        raise ContextError("run snapshot root is invalid")
    listed_paths = {entry["path"] for entry in entries}
    actual_snapshot_paths = {
        path.parent.relative_to(run_root).as_posix()
        for path in snapshots_root.glob("*/*/snapshot.json")
        if path.is_file() and not path.is_symlink()
    }
    if actual_snapshot_paths != listed_paths:
        raise ContextError("run snapshots do not match its manifest")
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
        _ensure_within(run_root, runs_root)
        if not run_root.is_dir() or run_root.is_symlink():
            raise ContextError("published run is not a regular directory")
        run = _validate_run_manifest(run_root)
        entries, expected_object_kinds = _validate_snapshot_entries(run)
        _validate_published_layout(run_root, entries, expected_object_kinds)
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
                    _TemporalRecord(timestamp) for timestamp in _timestamps(value)
                )
        except UnicodeError, json.JSONDecodeError:
            continue
    return records


def _record_intersects(record: _TemporalRecord, request: ContextRequest) -> bool:
    return request.start <= record.start < request.end


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
) -> ContextItem:
    all_records = [
        record for snapshot in snapshots for record in record_map[id(snapshot)]
    ]
    selected = any(_record_intersects(record, request) for record in all_records)
    roles: list[str] = []
    if selected:
        roles.append("in_range_record")
    if any(record.start < request.start for record in all_records):
        roles.append("prior_background")
    if any(record.start >= request.end for record in all_records):
        roles.append("later_development")
    reasons: list[str] = []
    if selected:
        reasons.append("source_record_in_range")
    if "prior_background" in roles:
        reasons.append("prior_background")
    if "later_development" in roles:
        reasons.append("later_evidence")
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
) -> dict[tuple[str, str, str, str], ContextItem]:
    items_by_key: dict[tuple[str, str, str, str], ContextItem] = {}
    for key, snapshots in grouped.items():
        all_records = [
            record for snapshot in snapshots for record in record_map[id(snapshot)]
        ]
        if not any(_record_intersects(record, request) for record in all_records):
            continue
        items_by_key[key] = _make_context_item(key, snapshots, record_map, request)
    return items_by_key


def extract_context(
    request: ContextRequest, runs: tuple[LoadedRun, ...]
) -> ContextExtractionResult:
    """Build a disposable result whose items directly reference loaded snapshots."""
    grouped, record_map = _group_context_snapshots(runs)
    items_by_key = _select_context_items(grouped, record_map, request)
    gaps: list[dict[str, str]] = []
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
                for gap in _snapshot_gaps(snapshot)
            )
    relations, unresolved, reference_gaps = _reference_data(tuple(items), runs)
    gaps.extend(reference_gaps)
    return ContextExtractionResult(
        request,
        runs,
        tuple(items),
        tuple(gaps),
        relations,
        unresolved,
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
]:
    targets: dict[
        tuple[str, str, str, str],
        dict[tuple[str, str, str, str], dict[str, str]],
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
                targets.setdefault(key, {})[logical_key] = target
    relations: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    gaps: list[dict[str, str]] = []
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
                            target["source_scope_id"],
                            target["source_id"],
                        ),
                    )
                    if matches:
                        for target in matches:
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
    return tuple(relations), tuple(unresolved), gaps


def _github_evidence_category(object_kind: str, name: str) -> str:
    if name == "issue.json":
        return "issue payload"
    if name == "pull-request.json":
        return "pull request payload"
    return f"GitHub {object_kind} source-native evidence"


def _contains_true_flag(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            (key in keys and child is True) or _contains_true_flag(child, keys)
            for key, child in value.items()
        )
    return isinstance(value, list) and any(
        _contains_true_flag(child, keys) for child in value
    )


def _snapshot_gaps(snapshot: LoadedSnapshot) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    for name, content in snapshot.evidence.items():
        if not name.endswith(".json"):
            continue
        value = json.loads(content)
        if _contains_true_flag(
            value, {"truncated", "isTruncated", "incomplete_results"}
        ):
            gaps.append({"kind": "source_truncated", "evidence_path": name})
    return gaps


def _opencode_evidence_category(name: str) -> str:
    return (
        "session export"
        if name == "session.json"
        else "OpenCode source-native evidence"
    )


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
                else _opencode_evidence_category(name)
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
