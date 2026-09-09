"""Offline Context Output generation from the published Raw Archive."""

from __future__ import annotations

import ctypes
import errno
import ipaddress
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
_SENSITIVE_TEXT = re.compile(
    r"(?:bearer\s+|basic\s+|[\"']?(?:access[_-]?token|api[_-]?key|"
    r"authorization|auth|cookie|credential|password|private[_-]?key|secret|"
    r"token)[\"']?\s*[=:]\s*"
    r"(?:(?:bearer|basic)\s+)?)"
    r"(?:[\"'][^\"']*[\"']|[^\s,;]+)",
    re.IGNORECASE,
)
_SENSITIVE_INPUT_KEY = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|auth|cookie|credential|"
    r"password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
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
    """Load the current Raw Archive while the caller keeps it immutable."""
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


def _snapshot_sort_key(snapshot: PublishedSnapshot) -> tuple[datetime, datetime, str]:
    collection_range = snapshot.run["collection_range"]

    def parse(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        return datetime.fromisoformat(normalized)

    return (
        parse(collection_range["from"]),
        parse(collection_range["to"]),
        snapshot.run["run_id"],
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
        ordered = tuple(sorted(snapshots, key=_snapshot_sort_key))
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


def _cleanup_staging(staging: Path) -> None:
    if not staging.exists():
        return
    try:
        shutil.rmtree(staging)
    except OSError:
        raise ContextError("context output cleanup failed") from None


def _sanitize_text(value: str) -> str:
    sensitive_query = re.compile(
        r"(?:access[_-]?token|api[_-]?key|auth|cookie|credential|password|"
        r"private[_-]?key|secret|token)",
        re.IGNORECASE,
    )

    def replace_url(match: re.Match[str]) -> str:
        url = match.group(0)
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError:
            return "[REDACTED_URL]"
        try:
            address = ipaddress.ip_address(hostname) if hostname else None
        except ValueError:
            address = None
        private_host = (
            hostname is None
            or hostname == "localhost"
            or hostname.endswith(
                (".local", ".internal", ".example", ".invalid", ".test")
            )
            or hostname.startswith("private.")
            or (address is not None and (address.is_private or address.is_loopback))
        )
        query_has_secret = any(
            sensitive_query.search(key) is not None
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
        if (
            parsed.scheme != "file"
            and not private_host
            and username is None
            and password is None
            and not query_has_secret
        ):
            return url
        return "[REDACTED_URL]"

    value = _URL.sub(replace_url, value)
    return _SENSITIVE_TEXT.sub("[REDACTED]", value)


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _front_matter(fields: tuple[tuple[str, Any], ...]) -> list[str]:
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


def _redact_whitelisted_value(value: Any) -> Any:
    """Redact strings inside a field already selected by a source renderer."""
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_INPUT_KEY.search(str(key)) is not None
                else _redact_whitelisted_value(entry)
            )
            for key, entry in value.items()
        }
    if isinstance(value, list):
        return [_redact_whitelisted_value(entry) for entry in value]
    if isinstance(value, tuple):
        return [_redact_whitelisted_value(entry) for entry in value]
    return value


def _fenced(value: str, language: str) -> list[str]:
    fence = "```"
    while fence in value:
        fence += "`"
    return [f"{fence}{language}", value, fence]


def _write_markdown(path: Path, lines: list[str]) -> None:
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
        key: _redact_whitelisted_value(coverage[key])
        for key in allowed
        if key in coverage and isinstance(coverage[key], (str, int, bool))
    }


def _render_provenance(lines: list[str], item: ContextItem) -> None:
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


def _session_context(item: ContextItem) -> dict[str, str]:
    projection = item.opencode
    if projection is None:
        return {}
    value = projection.session.get("value")
    value = value if isinstance(value, dict) else {}
    info = value.get("info")
    info = info if isinstance(info, dict) else {}
    context: dict[str, str] = {}
    for output_key, keys in (
        ("working_directory", ("directory",)),
        (
            "main_worktree_directory",
            ("worktree", "worktreeDirectory", "mainWorktree"),
        ),
    ):
        for source in (info, value):
            for key in keys:
                candidate = source.get(key)
                if isinstance(candidate, str) and candidate:
                    context[output_key] = candidate
                    break
            if output_key in context:
                break
    for snapshot in reversed(item.snapshots):
        metadata = snapshot.manifest.get("metadata")
        session = metadata.get("session") if isinstance(metadata, dict) else None
        if not isinstance(session, dict):
            continue
        for key, output_key in (
            ("directory", "working_directory"),
            ("worktree", "main_worktree_directory"),
        ):
            candidate = session.get(key)
            if isinstance(candidate, str) and candidate and output_key not in context:
                context[output_key] = candidate
    return context


def _part_value(part: dict[str, Any]) -> dict[str, Any]:
    value = part.get("value")
    return value if isinstance(value, dict) else {}


def _item_front_matter(
    item: ContextItem,
    result: ContextExtractionResult,
    source_kind: str,
    projection: Any,
) -> list[str]:
    source = item.source
    return _front_matter(
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


def _render_opencode(  # noqa: PLR0915
    item: ContextItem, result: ContextExtractionResult
) -> list[str]:
    assert item.opencode is not None
    projection = item.opencode
    context = _session_context(item)
    lines = _item_front_matter(item, result, "opencode", projection)
    lines.extend(["# OpenCode Session", ""])
    if context:
        lines.extend(["## Session Context", ""])
        labels = {
            "working_directory": "Working directory",
            "main_worktree_directory": "Main worktree directory",
        }
        for key in ("working_directory", "main_worktree_directory"):
            if key in context:
                lines.append(f"- {labels[key]}: `{_sanitize_text(context[key])}`")
        lines.append("")
    _render_provenance(lines, item)
    for message in projection.messages:
        role_value = message.get("role")
        role = str(role_value) if isinstance(role_value, str) else "unknown"
        created = message.get("created", "unknown")
        roles = ", ".join(message.get("temporal_roles", ())) or "observed_state"
        lines.extend(
            [
                f"## {role.title()} - {created}",
                "",
                f"Message: `{message['id']}`",
                f"Temporal roles: {roles}",
                "",
            ]
        )
        for part in message.get("parts", ()):
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            raw = _part_value(part)
            part_id = part.get("id", "unknown")
            part_roles = ", ".join(part.get("temporal_roles", ())) or "observed_state"
            if part_type == "text":
                text = raw.get("text", part.get("text"))
                if isinstance(text, str):
                    lines.extend(
                        [
                            f"### Text - `{part_id}`",
                            "",
                            f"Temporal roles: {part_roles}",
                            "",
                        ]
                    )
                    lines.extend(_fenced(_sanitize_text(text), "text"))
                    lines.append("")
            elif part_type in {"tool", "task"}:
                state = raw.get("state")
                state = state if isinstance(state, dict) else {}
                tool_name = raw.get("tool", part_type)
                tool_name = tool_name if isinstance(tool_name, str) else part_type
                lines.extend(
                    [
                        f"### Tool - {tool_name} - `{part_id}`",
                        "",
                        f"Temporal roles: {part_roles}",
                    ]
                )
                status = state.get("status")
                if isinstance(status, str):
                    lines.append(f"Status: {status}")
                if isinstance(part.get("start"), str):
                    lines.append(f"Start: {part['start']}")
                if isinstance(part.get("end"), str):
                    lines.append(f"End: {part['end']}")
                elif part.get("completion") == "unknown":
                    lines.append("End: unknown")
                metadata = state.get("metadata")
                if isinstance(metadata, dict):
                    relationship = {
                        key: metadata[key]
                        for key in ("parentSessionId", "sessionId")
                        if isinstance(metadata.get(key), str)
                    }
                    if relationship:
                        redacted_relationship = json.dumps(
                            _redact_whitelisted_value(relationship), sort_keys=True
                        )
                        lines.append(f"Task relationship: `{redacted_relationship}`")
                tool_input = state.get("input", raw.get("input"))
                if tool_input is not None:
                    lines.extend(["", "#### Input", ""])
                    rendered_input = json.dumps(
                        _redact_whitelisted_value(tool_input),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    lines.extend(_fenced(rendered_input, "json"))
                lines.append("")
            elif part_type == "compaction" or raw.get("synthetic") is True:
                lines.extend(
                    [
                        f"### Supporting context - `{part_id}`",
                        "",
                        "Compaction or synthetic continuation; not independent "
                        "repeated work.",
                        "",
                    ]
                )
                tail_start = raw.get("tail_start_id")
                if isinstance(tail_start, str):
                    lines.append(f"Tail start message: `{_sanitize_text(tail_start)}`")
                    lines.append("")
    if projection.gaps:
        lines.extend(["## Gaps", ""])
        lines.extend(
            f"- `{gap['kind']}`: "
            f"`{json.dumps(_redact_whitelisted_value(gap), sort_keys=True)}`"
            for gap in projection.gaps
        )
        lines.append("")
    return lines


def _record_value(record: dict[str, Any]) -> dict[str, Any] | str | None:
    representations = record.get("representations")
    if not isinstance(representations, list) or not representations:
        return None
    value = representations[-1].get("value")
    return value if isinstance(value, (dict, str)) else None


def _github_actor(value: dict[str, Any]) -> str | None:
    for key in ("user", "actor", "author"):
        actor = value.get(key)
        if isinstance(actor, dict):
            login = actor.get("login")
            if isinstance(login, str):
                return login
    return None


def _github_times(value: dict[str, Any]) -> list[str]:
    return [
        value[key]
        for key in (
            "created_at",
            "updated_at",
            "submitted_at",
            "merged_at",
            "closed_at",
        )
        if isinstance(value.get(key), str)
    ]


def _github_observations(record: dict[str, Any]) -> str:
    representations = record.get("representations")
    if not isinstance(representations, list):
        return ""
    return ", ".join(
        f"observation {index}" for index, _ in enumerate(representations, start=1)
    )


def _render_github_record(lines: list[str], record: dict[str, Any]) -> None:
    kind = record.get("kind")
    value = _record_value(record)
    if kind == "aggregate-diff":
        if isinstance(value, str):
            lines.extend(
                [
                    "## Aggregate Diff",
                    "",
                    "This diff does not establish a fix or fixing commit.",
                    "",
                ]
            )
            lines.extend(_fenced(_sanitize_text(value), "diff"))
            lines.append("")
        return
    if not isinstance(value, dict):
        return
    if kind not in {
        "issue",
        "pull-request",
        "ordinary-comment",
        "review",
        "inline-comment",
        "review-thread",
        "timeline",
    }:
        return
    if kind == "timeline" and value.get("event") not in {
        "closed",
        "reopened",
        "merged",
        "labeled",
        "unlabeled",
        "locked",
        "unlocked",
        "ready_for_review",
        "converted_to_draft",
    }:
        return
    heading = {
        "issue": "Issue",
        "pull-request": "Pull Request",
        "ordinary-comment": "Comment",
        "review": "Review",
        "inline-comment": "Inline review comment",
        "review-thread": "Review thread",
        "timeline": "Lifecycle event",
    }[kind]
    lines.extend([f"## {heading} - `{record['native_id']}`", ""])
    if kind in {"issue", "pull-request"}:
        for label, key in (
            ("Title", "title"),
            ("State", "state"),
            ("Number", "number"),
        ):
            if isinstance(value.get(key), (str, int)):
                lines.append(f"- {label}: `{_sanitize_text(str(value[key]))}`")
        if value.get("merged") is True:
            lines.append("- Merged: `true`")
    actor = _github_actor(value)
    if actor is not None:
        lines.append(f"- Actor: `{_sanitize_text(actor)}`")
    lines.extend(f"- Timestamp: {timestamp}" for timestamp in _github_times(value))
    if isinstance(value.get("body"), str):
        lines.extend(["", "### Body", ""])
        lines.extend(_fenced(_sanitize_text(value["body"]), "text"))
    if kind == "review" and isinstance(value.get("state"), str):
        lines.append(f"- Review state: `{value['state']}`")
    if kind == "inline-comment":
        location = {
            key: value[key]
            for key in ("path", "line", "side", "start_line", "start_side")
            if isinstance(value.get(key), (str, int))
        }
        if location:
            lines.append(f"- Location: `{json.dumps(location, sort_keys=True)}`")
    if kind == "review-thread":
        lines.extend(
            f"- {key}: `{str(value[key]).lower()}`"
            for key in ("isResolved", "isOutdated")
            if isinstance(value.get(key), bool)
        )
    observations = _github_observations(record)
    if observations:
        lines.append(f"- Observed in: {observations}")
    lines.append("")


def _render_github(item: ContextItem, result: ContextExtractionResult) -> list[str]:
    assert item.github is not None
    projection = item.github
    lines = _item_front_matter(item, result, "github", projection)
    lines.extend(
        [
            "# GitHub Item",
            "",
            "Source-native thread state is current observed state, not proof of a fix.",
            "",
        ]
    )
    _render_provenance(lines, item)
    for record in projection.records:
        _render_github_record(lines, record)
    if projection.relations:
        lines.extend(["## Within-Item Structure", ""])
        for relation in projection.relations:
            lines.append(
                f"- `{relation['kind']}`: "
                f"`{relation['from_native_id']}` -> `{relation['to_native_id']}`"
            )
        lines.append("")
    return lines


def _root_gaps(items: tuple[ContextItem, ...]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    for item in items:
        if item.opencode is None:
            continue
        gaps.extend(
            {
                "source_kind": "opencode",
                "source_id": item.source.manifest["source_id"],
                **{key: _sanitize_text(str(value)) for key, value in gap.items()},
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
        lines = _render_github(item, result)
        view_name = "github.md"
    elif item.opencode is not None:
        lines = _render_opencode(item, result)
        view_name = "opencode.md"
    else:
        raise ContextError("context item has no source projection")
    view_path = f"{item.path}/{view_name}"
    _write_markdown(item_root / view_name, lines)
    return view_path


def _render_index(
    staging: Path,
    result: ContextExtractionResult,
    items: list[tuple[ContextItem, str]],
) -> None:
    gaps = _root_gaps(result.items)
    lines = _front_matter(
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
    _write_markdown(staging / "index.md", lines)


def _rename_without_replacement(source: Path, target: Path) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError, OSError:
        raise OSError(
            errno.ENOTSUP, "atomic no-replace publication is unavailable"
        ) from None
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.ENOSYS:
        raise OSError(
            errno.ENOTSUP, "atomic no-replace publication is unavailable"
        ) from None
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error))
    raise OSError(error, os.strerror(error))


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
        except KeyError, TypeError, UnicodeError, ValueError:
            raise ContextError("context output rendering failed") from None
        except OSError:
            raise ContextError("context output rendering failed") from None
        try:
            _rename_without_replacement(staging, target)
        except FileExistsError:
            raise ContextError("context output already exists") from None
        except OSError:
            raise ContextError("context output publication failed") from None
        return target
    finally:
        if staging is not None:
            _cleanup_staging(staging)


def generate_context(
    archive: str | Path, request: ContextRequest, output: str | Path
) -> Path:
    return render_context(extract_context(request, load_archive(archive)), output)
