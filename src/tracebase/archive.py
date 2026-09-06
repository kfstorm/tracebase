"""Filesystem primitives for Collection Run staging and publication."""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
_PATH_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class ArchiveError(ValueError):
    """Raised when an archive operation cannot satisfy its contract."""


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7 without requiring a newer uuid module."""

    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (
        (timestamp_ms << 80) | (7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    )
    return uuid.UUID(int=value)


def encode_path_id(source_id: str) -> str:
    """Encode a provider ID as unpadded, filesystem-safe Base64URL."""

    if not isinstance(source_id, str) or not source_id:
        raise ArchiveError("source ID must be a non-empty string")
    return (
        base64.urlsafe_b64encode(source_id.encode("utf-8")).decode("ascii").rstrip("=")
    )


def decode_path_id(path_id: str) -> str:
    """Decode a path ID and reject values outside the canonical encoding."""

    if (
        not isinstance(path_id, str)
        or not path_id
        or not _PATH_ID_PATTERN.fullmatch(path_id)
    ):
        raise ArchiveError("invalid path ID")
    padded = path_id + "=" * (-len(path_id) % 4)
    try:
        source_id = base64.urlsafe_b64decode(padded).decode("utf-8")
    except UnicodeDecodeError, ValueError:
        raise ArchiveError("invalid path ID") from None
    if encode_path_id(source_id) != path_id:
        raise ArchiveError("invalid path ID")
    return source_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CollectionRange:
    """A half-open range retaining the caller's original endpoint strings."""

    from_text: str
    to_text: str
    start: datetime
    end: datetime

    @classmethod
    def parse(cls, from_text: str, to_text: str) -> CollectionRange:
        start = cls._parse_endpoint(from_text)
        end = cls._parse_endpoint(to_text)
        if start >= end:
            raise ArchiveError("collection range requires from to be before to")
        return cls(from_text, to_text, start, end)

    @staticmethod
    def _parse_endpoint(value: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise ArchiveError("collection range endpoints require an explicit offset")
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise ArchiveError(
                "collection range endpoints must be ISO 8601 timestamps"
            ) from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ArchiveError("collection range endpoints require an explicit offset")
        return parsed

    def intersects(self, other: CollectionRange) -> bool:
        return self.start < other.end and other.start < self.end

    def as_manifest(self) -> dict[str, str]:
        return {"from": self.from_text, "to": self.to_text}


@dataclass(frozen=True)
class Snapshot:
    object_kind: str
    source_id: str
    observation_window: dict[str, str]
    evidence_files: tuple[dict[str, Any], ...] = ()
    selection_provenance: tuple[dict[str, Any], ...] = ()


class Archive:
    """Own the archive root and the published-run overlap registry."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def new_run_id(self) -> str:
        return str(uuid7())

    def create_staging(self, run_id: str) -> Path:
        staging_root = self.root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / run_id
        staging.mkdir()
        return staging

    def has_overlap(
        self,
        source_kind: str,
        scope_id: str,
        collection_range: CollectionRange,
    ) -> bool:
        runs_root = self.root / "runs"
        if not runs_root.is_dir():
            return False
        for manifest_path in runs_root.glob("*/run.json"):
            manifest = self._read_manifest(manifest_path)
            if manifest is None:
                continue
            source = manifest.get("source")
            range_data = manifest.get("collection_range")
            if not isinstance(source, dict) or not isinstance(range_data, dict):
                continue
            if source.get("kind") != source_kind or source.get("scope_id") != scope_id:
                continue
            try:
                published_range = CollectionRange.parse(
                    range_data["from"], range_data["to"]
                )
            except ArchiveError, KeyError, TypeError:
                continue
            if collection_range.intersects(published_range):
                return True
        return False

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any] | None:
        try:
            with path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except OSError, json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


class CollectionRun:
    """Build a complete run in staging, then publish it with one rename."""

    def __init__(
        self,
        archive: Archive,
        source_kind: str,
        scope_id: str,
        collection_range: CollectionRange,
        collector_version: str,
        effective_options: dict[str, Any],
        run_id: str | None = None,
    ):
        if not source_kind or not scope_id:
            raise ArchiveError("source kind and scope ID are required")
        self.archive = archive
        self.run_id = run_id or archive.new_run_id()
        self.source_kind = source_kind
        self.scope_id = scope_id
        self.collection_range = collection_range
        self.collector_version = collector_version
        self.effective_options = effective_options
        self.started_at = _utc_now()
        if archive.has_overlap(source_kind, scope_id, collection_range):
            raise ArchiveError("collection range overlaps a published run")
        self.staging = archive.create_staging(self.run_id)
        (self.staging / "snapshots").mkdir()
        self._snapshots: list[dict[str, Any]] = []

    def write_snapshot(self, snapshot: Snapshot) -> Path:
        snapshot_root = (
            self.staging
            / "snapshots"
            / snapshot.object_kind
            / encode_path_id(snapshot.source_id)
        )
        snapshot_root.mkdir(parents=True, exist_ok=False)
        manifest = {
            "format_version": FORMAT_VERSION,
            "object_kind": snapshot.object_kind,
            "source_id": snapshot.source_id,
            "observation_window": snapshot.observation_window,
            "evidence_files": list(snapshot.evidence_files),
        }
        provenance = list(snapshot.selection_provenance)
        if provenance:
            manifest["selection_provenance"] = provenance
        self._write_json(snapshot_root / "snapshot.json", manifest)
        relative_path = str(snapshot_root.relative_to(self.staging))
        entry: dict[str, Any] = {
            "object_kind": snapshot.object_kind,
            "source_id": snapshot.source_id,
            "path": relative_path,
        }
        if provenance:
            entry["selection_provenance"] = provenance
        self._snapshots.append(entry)
        return snapshot_root

    def write_evidence(
        self, snapshot_root: Path, relative_path: str, content: bytes
    ) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArchiveError("evidence path must stay within the Snapshot")
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def publish(self, coverage: dict[str, Any]) -> Path:
        if self.archive.has_overlap(
            self.source_kind, self.scope_id, self.collection_range
        ):
            raise ArchiveError("collection range overlaps a published run")
        runs_root = self.archive.root / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        published = runs_root / self.run_id
        if published.exists():
            raise ArchiveError("run ID already exists")
        manifest = {
            "format_version": FORMAT_VERSION,
            "run_id": self.run_id,
            "source": {"kind": self.source_kind, "scope_id": self.scope_id},
            "collection_range": self.collection_range.as_manifest(),
            "started_at": self.started_at,
            "completed_at": _utc_now(),
            "collector": {
                "version": self.collector_version,
                "effective_options": self.effective_options,
            },
            "coverage": coverage,
            "snapshots": self._snapshots,
        }
        self._write_json(self.staging / "run.json", manifest)
        self.staging.rename(published)
        return published

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
