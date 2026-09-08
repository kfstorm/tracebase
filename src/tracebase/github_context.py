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
    source = value.get("user", value.get("actor"))
    login = source.get("login") if isinstance(source, dict) else None
    return {"login": login} if isinstance(login, str) else None


def _timestamps(value: dict[str, Any]) -> dict[str, str]:
    timestamps = {
        name: value[name]
        for name in ("created_at", "updated_at", "submitted_at")
        if isinstance(value.get(name), str)
    }
    if value.get("event") == "committed":
        author = value.get("author")
        committer = value.get("committer")
        if isinstance(author, dict) and isinstance(author.get("date"), str):
            timestamps["author_date"] = author["date"]
        if isinstance(committer, dict) and isinstance(committer.get("date"), str):
            timestamps["committer_date"] = committer["date"]
    return timestamps


def _occurrence_times(value: dict[str, Any]) -> tuple[datetime, ...]:
    timestamps = _timestamps(value)
    candidates: tuple[str | None, ...]
    if value.get("event") == "committed":
        candidates = (timestamps.get("committer_date"), timestamps.get("author_date"))
        for candidate in candidates:
            if (timestamp := _timestamp(candidate)) is not None:
                return (timestamp,)
        return ()
    else:
        candidates = (
            timestamps.get("created_at"),
            timestamps.get("updated_at"),
            timestamps.get("submitted_at"),
        )
    return tuple(
        timestamp
        for value in candidates
        if (timestamp := _timestamp(value)) is not None
    )


def _record(
    kind: str,
    native_id: str,
    value: dict[str, Any],
    evidence_path: str,
    snapshot: PublishedSnapshot,
) -> dict[str, Any]:
    timestamps = _timestamps(value)
    result: dict[str, Any] = {
        "kind": kind,
        "native_id": native_id,
        "timestamps": timestamps,
        "representations": [
            {
                "evidence_path": evidence_path,
                "run_id": snapshot.run["run_id"],
                "observation_window": snapshot.manifest["observation_window"],
                "value": value,
            }
        ],
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


def _roles(record: dict[str, Any], start: datetime, end: datetime) -> tuple[str, ...]:
    timestamps = [
        timestamp
        for representation in record["representations"]
        if isinstance(representation["value"], dict)
        for timestamp in _occurrence_times(representation["value"])
    ]
    if timestamps:
        return tuple(
            role
            for role, present in (
                (
                    "in_range_work",
                    any(start <= timestamp < end for timestamp in timestamps),
                ),
                (
                    "earlier_background",
                    any(timestamp < start for timestamp in timestamps),
                ),
                (
                    "later_progression",
                    any(timestamp >= end for timestamp in timestamps),
                ),
            )
            if present
        )
    return ("observed_state",)


def _record_sort_key(record: dict[str, Any]) -> tuple[int, float, str, str]:
    timestamps = [
        timestamp
        for representation in record["representations"]
        if isinstance(representation["value"], dict)
        for timestamp in _occurrence_times(representation["value"])
    ]
    if timestamps:
        return (
            0,
            min(timestamp.timestamp() for timestamp in timestamps),
            record["kind"],
            record["native_id"],
        )
    return (1, 0.0, record["kind"], record["native_id"])


def project_github(  # noqa: PLR0915
    snapshots: tuple[PublishedSnapshot, ...], start: datetime, end: datetime
) -> GitHubProjection:
    """Project archived GitHub payloads without inferring causal history."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    relations: set[tuple[str, str, str]] = set()
    gaps: set[tuple[str, str]] = set()
    source_id = snapshots[0].manifest["source_id"]
    object_kind = snapshots[0].manifest["object_kind"]
    for snapshot in snapshots:
        inline_nodes_by_rest_id: dict[int, str] = {}
        inline_replies: list[tuple[int, str]] = []
        issue = _json(snapshot, "issue.json")
        if not isinstance(issue, dict) or issue.get("node_id") != source_id:
            raise ArchiveError("GitHub Item evidence identity was invalid")
        _add_record(
            records, _record(object_kind, source_id, issue, "issue.json", snapshot)
        )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("comments.")
        ):
            for value in _list(snapshot, path):
                if isinstance(value.get("id"), int):
                    _add_record(
                        records,
                        _record(
                            "ordinary-comment", str(value["id"]), value, path, snapshot
                        ),
                    )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("timeline.")
        ):
            for value in _list(snapshot, path):
                identifier = value.get("node_id", value.get("id"))
                if isinstance(identifier, (str, int)):
                    kind = "timeline"
                    if value.get("event") == "commented" and isinstance(
                        value.get("id"), int
                    ):
                        kind = "ordinary-comment"
                    elif value.get("event") == "reviewed" and isinstance(
                        value.get("id"), int
                    ):
                        kind, identifier = "review", value["id"]
                    _add_record(
                        records, _record(kind, str(identifier), value, path, snapshot)
                    )
                    source = value.get("source")
                    source_issue = (
                        source.get("issue") if isinstance(source, dict) else None
                    )
                    cross_reference_source_id = (
                        source_issue.get("node_id")
                        if isinstance(source_issue, dict)
                        else None
                    )
                    if value.get("event") == "cross-referenced" and isinstance(
                        cross_reference_source_id, str
                    ):
                        relations.add(
                            (
                                "cross-referenced",
                                cross_reference_source_id,
                                snapshots[0].manifest["source_id"],
                            )
                        )
        if object_kind != "pull-request":
            continue
        pull = _json(snapshot, "pull-request.json")
        if not isinstance(pull, dict) or pull.get("node_id") != source_id:
            raise ArchiveError("GitHub Pull Request evidence identity was invalid")
        _add_record(
            records,
            _record(
                "pull-request-payload",
                source_id,
                pull,
                "pull-request.json",
                snapshot,
            ),
        )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("reviews.")
        ):
            for value in _list(snapshot, path):
                if isinstance(value.get("id"), int):
                    _add_record(
                        records,
                        _record("review", str(value["id"]), value, path, snapshot),
                    )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("review-comments.")
        ):
            for value in _list(snapshot, path):
                node_id = value.get("node_id")
                if not isinstance(node_id, str):
                    raise ArchiveError("GitHub review-comment evidence was invalid")
                _add_record(
                    records, _record("inline-comment", node_id, value, path, snapshot)
                )
                if isinstance(value.get("id"), int):
                    inline_nodes_by_rest_id[value["id"]] = node_id
                review_id = value.get("pull_request_review_id")
                if isinstance(review_id, int):
                    relations.add(("review-inline-comment", str(review_id), node_id))
                reply_id = value.get("in_reply_to_id")
                if isinstance(reply_id, int):
                    inline_replies.append((reply_id, node_id))
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("review-threads.")
        ):
            for thread in _thread_nodes(snapshot, path):
                thread_id = thread.get("id")
                comments = thread.get("comments")
                nodes = comments.get("nodes") if isinstance(comments, dict) else None
                if not isinstance(thread_id, str) or not isinstance(nodes, list):
                    raise ArchiveError("GitHub review-thread evidence was invalid")
                _add_record(
                    records,
                    _record("review-thread", thread_id, thread, path, snapshot),
                )
                for comment in nodes:
                    if not isinstance(comment, dict) or not isinstance(
                        comment.get("id"), str
                    ):
                        raise ArchiveError("GitHub review-thread evidence was invalid")
                    relations.add(("thread-inline-comment", thread_id, comment["id"]))
        for path in sorted(
            name
            for name in snapshot.evidence
            if name.startswith("review-thread-comments.")
        ):
            thread_page = _json(snapshot, path)
            if not isinstance(thread_page, dict):
                raise ArchiveError("GitHub review-thread comments were invalid")
            try:
                node = thread_page["data"]["node"]
                thread_id, nodes = node["id"], node["comments"]["nodes"]
            except KeyError, TypeError:
                raise ArchiveError(
                    "GitHub review-thread comments were invalid"
                ) from None
            if not isinstance(thread_id, str) or not isinstance(nodes, list):
                raise ArchiveError("GitHub review-thread comments were invalid")
            for comment in nodes:
                if not isinstance(comment, dict) or not isinstance(
                    comment.get("id"), str
                ):
                    raise ArchiveError("GitHub review-thread comments were invalid")
                relations.add(("thread-inline-comment", thread_id, comment["id"]))
        for reply_id, node_id in inline_replies:
            parent = inline_nodes_by_rest_id.get(reply_id)
            if parent is not None:
                relations.add(("inline-reply", parent, node_id))
        if "pull-request.diff" in snapshot.evidence:
            _add_record(
                records,
                {
                    "kind": "aggregate-diff",
                    "native_id": source_id,
                    "timestamps": {},
                    "limitations": ["does_not_establish_fix_or_commit"],
                    "representations": [
                        {
                            "evidence_path": "pull-request.diff",
                            "run_id": snapshot.run["run_id"],
                            "observation_window": snapshot.manifest[
                                "observation_window"
                            ],
                            "value": snapshot.evidence["pull-request.diff"].decode(
                                "utf-8"
                            ),
                        }
                    ],
                },
            )
    ordered = tuple(
        sorted(
            records.values(),
            key=_record_sort_key,
        )
    )
    selected = [
        record for record in ordered if "in_range_work" in _roles(record, start, end)
    ]
    for record in ordered:
        roles = _roles(record, start, end)
        record["temporal_roles"] = roles
        record["inclusion_reasons"] = (
            ("in_range_source_record",)
            if "in_range_work" in roles
            else ("bounded_item_context",)
        )
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
