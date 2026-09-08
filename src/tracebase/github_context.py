"""Deterministic GitHub Context Output projection from archived evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .archive import ArchiveError, PublishedSnapshot


@dataclass(frozen=True, slots=True)
class GitHubProjection:
    """Source-native GitHub records and native-ID relations for one Item."""

    selected: bool
    inclusion_reasons: tuple[str, ...]
    temporal_roles: tuple[str, ...]
    records: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, str], ...]
    gaps: tuple[dict[str, str], ...]


def _json(snapshot: PublishedSnapshot, path: str) -> dict[str, Any] | list[Any]:
    try:
        value = json.loads(snapshot.evidence[path])
    except KeyError, UnicodeDecodeError, json.JSONDecodeError:
        raise ArchiveError("GitHub context evidence was invalid") from None
    if not isinstance(value, (dict, list)):
        raise ArchiveError("GitHub context evidence was invalid")
    return value


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _actor(value: dict[str, Any]) -> dict[str, str] | None:
    user = value.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return {"login": login} if isinstance(login, str) else None


def _record(
    kind: str, native_id: str, value: dict[str, Any], evidence_path: str
) -> dict[str, Any]:
    timestamps = {
        name: value[name]
        for name in ("created_at", "updated_at", "submitted_at")
        if isinstance(value.get(name), str)
    }
    result: dict[str, Any] = {
        "kind": kind,
        "native_id": native_id,
        "timestamps": timestamps,
        "representations": [{"evidence_path": evidence_path, "value": value}],
    }
    actor = _actor(value)
    if actor is not None:
        result["actor"] = actor
    return result


def _add_record(
    records: dict[tuple[str, str], dict[str, Any]], record: dict[str, Any]
) -> None:
    key = (record["kind"], record["native_id"])
    prior = records.get(key)
    if prior is None:
        records[key] = record
        return
    prior["representations"].extend(record["representations"])
    for name, value in record["timestamps"].items():
        prior["timestamps"].setdefault(name, value)


def _list(snapshot: PublishedSnapshot, path: str) -> list[dict[str, Any]]:
    value = _json(snapshot, path)
    if not isinstance(value, list) or not all(
        isinstance(entry, dict) for entry in value
    ):
        raise ArchiveError("GitHub context evidence was invalid")
    return value


def _thread_nodes(snapshot: PublishedSnapshot, path: str) -> list[dict[str, Any]]:
    value = _json(snapshot, path)
    if not isinstance(value, dict):
        raise ArchiveError("GitHub review-thread evidence was invalid")
    try:
        nodes = value["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except KeyError, TypeError:
        raise ArchiveError("GitHub review-thread evidence was invalid") from None
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise ArchiveError("GitHub review-thread evidence was invalid")
    return nodes


def _in_range(value: str, start: datetime, end: datetime) -> bool:
    timestamp = _timestamp(value)
    return timestamp is not None and start <= timestamp < end


def _roles(record: dict[str, Any], start: datetime, end: datetime) -> tuple[str, ...]:
    timestamps = record["timestamps"]
    if any(_in_range(value, start, end) for value in timestamps.values()):
        return ("in_range_work",)
    if timestamps:
        return ("earlier_background", "later_progression")
    return ("observed_state",)


def project_github(
    snapshots: tuple[PublishedSnapshot, ...], start: datetime, end: datetime
) -> GitHubProjection:
    """Project archived GitHub payloads without inferring causal history."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    relations: set[tuple[str, str, str]] = set()
    gaps: set[tuple[str, str]] = set()
    source_id = snapshots[0].manifest["source_id"]
    object_kind = snapshots[0].manifest["object_kind"]
    for snapshot in snapshots:
        issue = _json(snapshot, "issue.json")
        if not isinstance(issue, dict) or issue.get("node_id") != source_id:
            raise ArchiveError("GitHub Item evidence identity was invalid")
        _add_record(records, _record(object_kind, source_id, issue, "issue.json"))
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("comments.")
        ):
            for value in _list(snapshot, path):
                if isinstance(value.get("id"), int):
                    _add_record(
                        records,
                        _record("ordinary-comment", str(value["id"]), value, path),
                    )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("timeline.")
        ):
            for value in _list(snapshot, path):
                identifier = value.get("node_id", value.get("id"))
                if isinstance(identifier, (str, int)):
                    _add_record(
                        records, _record("timeline", str(identifier), value, path)
                    )
                    if isinstance(value.get("id"), int):
                        relations.add(
                            ("timeline-mirror", str(value["id"]), str(value["id"]))
                        )
        if object_kind != "pull-request":
            continue
        pull = _json(snapshot, "pull-request.json")
        if not isinstance(pull, dict) or pull.get("node_id") != source_id:
            raise ArchiveError("GitHub Pull Request evidence identity was invalid")
        _add_record(
            records,
            _record("pull-request-payload", source_id, pull, "pull-request.json"),
        )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("reviews.")
        ):
            for value in _list(snapshot, path):
                if isinstance(value.get("id"), int):
                    _add_record(
                        records, _record("review", str(value["id"]), value, path)
                    )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("review-comments.")
        ):
            for value in _list(snapshot, path):
                node_id = value.get("node_id")
                if not isinstance(node_id, str):
                    raise ArchiveError("GitHub review-comment evidence was invalid")
                _add_record(records, _record("inline-comment", node_id, value, path))
                review_id = value.get("pull_request_review_id")
                if isinstance(review_id, int):
                    relations.add(("review-inline-comment", str(review_id), node_id))
                reply_id = value.get("in_reply_to_id")
                if isinstance(reply_id, int):
                    relations.add(("inline-reply", str(reply_id), node_id))
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("review-threads.")
        ):
            for thread in _thread_nodes(snapshot, path):
                thread_id = thread.get("id")
                comments = thread.get("comments")
                nodes = comments.get("nodes") if isinstance(comments, dict) else None
                if not isinstance(thread_id, str) or not isinstance(nodes, list):
                    raise ArchiveError("GitHub review-thread evidence was invalid")
                _add_record(records, _record("review-thread", thread_id, thread, path))
                for comment in nodes:
                    if not isinstance(comment, dict) or not isinstance(
                        comment.get("id"), str
                    ):
                        raise ArchiveError("GitHub review-thread evidence was invalid")
                    relations.add(("thread-inline-comment", thread_id, comment["id"]))
        if "pull-request.diff" in snapshot.evidence:
            gaps.add(("aggregate_diff", "does_not_establish_fix_or_commit"))
    ordered = tuple(
        sorted(
            records.values(),
            key=lambda record: (
                min(record["timestamps"].values(), default=""),
                record["kind"],
                record["native_id"],
            ),
        )
    )
    selected = [
        record for record in ordered if "in_range_work" in _roles(record, start, end)
    ]
    return GitHubProjection(
        bool(selected),
        ("in_range_source_record",) if selected else (),
        tuple(
            sorted({role for record in ordered for role in _roles(record, start, end)})
        ),
        ordered,
        tuple(
            {"kind": kind, "from_native_id": left, "to_native_id": right}
            for kind, left, right in sorted(relations)
        ),
        tuple({"kind": kind, "detail": detail} for kind, detail in sorted(gaps)),
    )
