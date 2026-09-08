"""Offline Context Output generation from the published Raw Archive."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
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

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|z|[+-]\d{2}:\d{2})$"
)
_AT_FDCWD = -2 if sys.platform == "darwin" else -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 4
_WINDOWS_EXISTS_ERRORS = {80, 183}


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

    @property
    def source(self) -> PublishedSnapshot:
        return self.snapshots[0]


@dataclass(frozen=True, slots=True)
class ContextExtractionResult:
    request: ContextRequest
    runs: tuple[PublishedRun, ...]
    items: tuple[ContextItem, ...]
    relations: tuple[dict[str, Any], ...] = ()


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


def _window_intersects(snapshot: PublishedSnapshot, request: ContextRequest) -> bool:
    window = snapshot.manifest["observation_window"]
    start = ContextRequest._endpoint(window["from"])
    end = ContextRequest._endpoint(window["to"])
    return start < request.end and request.start < end


def extract_context(
    request: ContextRequest, runs: tuple[PublishedRun, ...]
) -> ContextExtractionResult:
    """Group Archive-level observations without provider-native interpretation."""
    grouped: dict[tuple[str, str, str, str], list[PublishedSnapshot]] = {}
    for run in runs:
        for snapshot in run.snapshots:
            if _window_intersects(snapshot, request):
                grouped.setdefault(_logical_key(snapshot), []).append(snapshot)
    items = tuple(
        ContextItem(
            tuple(sorted(snapshots, key=lambda value: value.run["run_id"])),
            _item_path(key),
        )
        for key, snapshots in sorted(grouped.items())
    )
    return ContextExtractionResult(request, runs, items)


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


def _publish_posix_exclusive(
    library_name: str | None, symbol: str, flag: int, staging: Path, target: Path
) -> None:
    library = ctypes.CDLL(library_name, use_errno=True)
    rename = getattr(library, symbol, None)
    if rename is None:
        raise ContextError("atomic context publication is unavailable")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if (
        rename(
            _AT_FDCWD,
            str(staging).encode(),
            _AT_FDCWD,
            str(target).encode(),
            flag,
        )
        == 0
    ):
        return
    if ctypes.get_errno() == errno.EEXIST:
        raise ContextError("context output already exists")
    raise OSError("atomic context publication failed")


def _publish_windows_exclusive(staging: Path, target: Path) -> None:
    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW  # type: ignore[attr-defined]
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move_file.restype = ctypes.c_int
    if move_file(str(staging), str(target), 0):
        return
    if ctypes.get_last_error() in _WINDOWS_EXISTS_ERRORS:  # type: ignore[attr-defined]
        raise ContextError("context output already exists")
    raise OSError("atomic context publication failed")


def _publish_staging(staging: Path, target: Path) -> None:
    """Atomically publish without replacing a path created after validation."""
    if os.name == "nt":
        _publish_windows_exclusive(staging, target)
        return
    if sys.platform == "darwin":
        _publish_posix_exclusive(
            "/usr/lib/libSystem.B.dylib", "renameatx_np", _RENAME_EXCL, staging, target
        )
        return
    _publish_posix_exclusive(None, "renameat2", _RENAME_NOREPLACE, staging, target)


def _render_source_view(item_root: Path, item: ContextItem) -> str:
    """Render the shared source-view shell; source projections extend this seam."""
    source = item.source
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
        observation_name = f"{observation_index:03d}-{snapshot.run['run_id']}"
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
        observation_name = f"{observation_index:03d}-{run_id}"
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
    view_path = _render_source_view(item_root, item)
    return {
        "source_kind": source.manifest["source_kind"],
        "source_scope_id": source.run["source"]["scope_id"],
        "object_kind": source.manifest["object_kind"],
        "source_id": source.manifest["source_id"],
        "path": item.path,
        "view_path": view_path,
        "provenance": provenance,
    }


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
            for item in items
        )
    else:
        lines.append("No source items were selected.")
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
            "relations": list(result.relations),
            "unresolved_references": [],
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
        _publish_staging(staging, target)
        return target
    except ContextError:
        raise
    except OSError, TypeError, ValueError:
        raise ContextError("context output publication failed") from None
    finally:
        _cleanup_staging(staging)


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    return render_context(extract_context(request, load_archive(archive)), output)
