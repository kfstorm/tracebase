"""Deterministic GitHub Context Output projection from archived evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive import ArchiveError, PublishedRun, PublishedSnapshot
from .context_adapter import (
    ContextOrdering,
    RenderedContextItem,
    rendered_context_item,
    safe_path_component,
)
from .context_semantics import (
    ACTOR_SCOPED_ATTRIBUTION_POLICY,
    PROVIDER_EVIDENCE_POLICY,
)
from .github_identity import GitHubIdentity, require_github_identity

if TYPE_CHECKING:
    from .context import ContextExtractionResult, ContextItem


@dataclass(frozen=True, slots=True)
class GitHubProjection:
    """Source-native GitHub records and within-Item native structure."""

    selected: bool
    records: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, str], ...]
    repository: str
    number: int
    title: str | None
    tracked_login: str | None
    tracked_identity: GitHubIdentity | None = None


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
    source = value.get("actor")
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


def _record_values(record: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(
        value
        for representation in record.get("representations", ())
        if isinstance(representation, dict)
        for value in (representation.get("value"),)
        if isinstance(value, dict)
    )


def github_actor_login(value: dict[str, Any]) -> str | None:
    for key in ("user", "actor", "author"):
        actor = value.get(key)
        login = actor.get("login") if isinstance(actor, dict) else None
        if isinstance(login, str):
            return login
    return None


def github_logins_match(login: Any, tracked_login: str | None) -> bool:
    return (
        isinstance(login, str)
        and tracked_login is not None
        and login.casefold() == tracked_login.casefold()
    )


def github_inline_comment_canonical_id(value: dict[str, Any]) -> str | None:
    """Return the GraphQL node ID shared by REST and thread representations."""
    node_id = value.get("node_id")
    if isinstance(node_id, str) and node_id:
        return node_id
    identifier = value.get("id")
    return identifier if isinstance(identifier, str) and identifier else None


def _record_is_in_range(record: dict[str, Any], start: datetime, end: datetime) -> bool:
    return any(
        start <= timestamp < end
        for value in _record_values(record)
        for timestamp in _occurrence_times(value)
    )


def _commit_is_user_authored(
    value: dict[str, Any], tracked_login: str | None, identity: GitHubIdentity | None
) -> bool:
    author = value.get("author")
    if isinstance(author, dict) and github_logins_match(
        author.get("login"), tracked_login
    ):
        return True
    email = author.get("email") if isinstance(author, dict) else None
    return identity is not None and identity.matches_commit_email(email)


def github_user_work_record_ids(
    projection: GitHubProjection, start: datetime, end: datetime
) -> frozenset[tuple[str, str]]:
    """Select GitHub records that can enter the user's work projection.

    The source item itself is not ownership evidence. Object records therefore
    count only when the tracked account authored the object during the range;
    collaborator updates remain context-only evidence.
    """
    selected: set[tuple[str, str]] = set()
    for record in projection.records:
        kind = record.get("kind")
        native_id = record.get("native_id")
        values = _record_values(record)
        if not isinstance(kind, str) or not isinstance(native_id, str) or not values:
            continue
        user_work = False
        if kind in {"issue", "pull-request", "pull-request-payload"}:
            user_work = any(
                github_logins_match(
                    value.get("user", {}).get("login"), projection.tracked_login
                )
                and (
                    (created := _timestamp(value.get("created_at"))) is not None
                    and start <= created < end
                )
                for value in values
                if isinstance(value.get("user"), dict)
            )
        elif kind == "timeline" and any(
            value.get("event") == "committed" for value in values
        ):
            user_work = any(
                _commit_is_user_authored(
                    value, projection.tracked_login, projection.tracked_identity
                )
                and _record_is_in_range(
                    {"representations": [{"value": value}]}, start, end
                )
                for value in values
            )
        elif kind not in {"aggregate-diff", "review-thread", "ordinary-comment-alias"}:
            user_work = any(
                github_logins_match(github_actor_login(value), projection.tracked_login)
                and _record_is_in_range(
                    {"representations": [{"value": value}]}, start, end
                )
                for value in values
            )
        if user_work:
            selected.add((kind, native_id))
    return frozenset(selected)


def _record(
    kind: str,
    native_id: str,
    value: dict[str, Any],
    evidence_path: str,
    snapshot: PublishedSnapshot,
    source_order: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "native_id": native_id,
        "representations": [
            {
                "evidence_path": evidence_path,
                "run_id": snapshot.run["run_id"],
                "value": value,
            }
        ],
    }
    actor = _actor(value)
    if actor is not None:
        result["actor"] = actor
    if source_order is not None:
        result["source_order"] = source_order
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


def _merge_thread_comments(value: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    comments = value.get("comments")
    if not isinstance(comments, dict):
        comments = {}
        value["comments"] = comments
    existing = comments.get("nodes")
    if not isinstance(existing, list):
        existing = []
        comments["nodes"] = existing
    positions = {
        node.get("id"): index
        for index, node in enumerate(existing)
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        position = positions.get(node_id)
        if position is None:
            positions[node_id] = len(existing)
            existing.append(node)
        elif isinstance(node, dict) and len(node) > len(existing[position]):
            existing[position] = node


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


def _record_sort_key(
    record: dict[str, Any], start: datetime, end: datetime
) -> tuple[int, float, str, str]:
    timestamps = [
        timestamp
        for representation in record["representations"]
        if isinstance(representation["value"], dict)
        for timestamp in _occurrence_times(representation["value"])
    ]
    if timestamps:
        in_range = [timestamp for timestamp in timestamps if start <= timestamp < end]
        relevant = in_range if in_range else timestamps
        return (
            0,
            min(timestamp.timestamp() for timestamp in relevant),
            record["kind"],
            record["native_id"],
        )
    return (1, 0.0, record["kind"], record["native_id"])


def project_github(  # noqa: PLR0915
    selected_snapshot: PublishedSnapshot,
    start: datetime,
    end: datetime,
) -> GitHubProjection:
    """Project archived GitHub payloads without inferring causal history."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    relations: set[tuple[str, str, str]] = set()
    source_id = selected_snapshot.manifest["source_id"]
    object_kind = selected_snapshot.manifest["object_kind"]
    repository = "unknown/unknown"
    number = 0
    title: str | None = None
    effective_options = selected_snapshot.run.get("collector", {}).get(
        "effective_options", {}
    )
    tracked_login = (
        effective_options.get("actor_login")
        if isinstance(effective_options, dict)
        else None
    )
    if not isinstance(tracked_login, str) or not tracked_login:
        tracked_login = None
    paged_thread_comments: list[tuple[str, list[dict[str, Any]]]] = []
    timeline_order = 0

    inline_nodes_by_rest_id: dict[int, str] = {}
    inline_replies: list[tuple[int, str]] = []
    snapshot = selected_snapshot
    issue = _json(snapshot, "issue.json")
    if not isinstance(issue, dict) or issue.get("node_id") != source_id:
        raise ArchiveError("GitHub Item evidence identity was invalid")
    repository_value = issue.get("repository_url")
    if isinstance(repository_value, str) and repository_value:
        repository = (
            repository_value.removeprefix("https://api.github.com/repos/")
            .removeprefix("https://github.com/")
            .strip("/")
        )
    if isinstance(issue.get("number"), int):
        number = issue["number"]
    if isinstance(issue.get("title"), str):
        title = issue["title"]
    _add_record(
        records,
        _record(object_kind, source_id, issue, "issue.json", snapshot),
    )
    for path in sorted(
        name for name in snapshot.evidence if name.startswith("comments.")
    ):
        for value in _list(snapshot, path):
            if isinstance(value.get("id"), int):
                comment_id = str(value["id"])
                _add_record(
                    records,
                    _record(
                        "ordinary-comment",
                        comment_id,
                        value,
                        path,
                        snapshot,
                    ),
                )
                node_id = value.get("node_id")
                if isinstance(node_id, str):
                    records.setdefault(
                        ("ordinary-comment-alias", node_id),
                        {"kind": "ordinary-comment-alias", "native_id": node_id},
                    )["canonical_id"] = comment_id
    for path in sorted(
        name for name in snapshot.evidence if name.startswith("timeline.")
    ):
        for value in _list(snapshot, path):
            identifier = value.get("node_id", value.get("id"))
            if isinstance(identifier, (str, int)):
                kind = "timeline"
                if value.get("event") == "commented":
                    kind = "ordinary-comment"
                    alias = value.get("node_id")
                    if isinstance(alias, str):
                        alias_record = records.get(("ordinary-comment-alias", alias))
                        identifier = (
                            alias_record.get("canonical_id")
                            if alias_record is not None
                            else alias
                        )
                    elif isinstance(value.get("id"), int):
                        identifier = value["id"]
                elif value.get("event") == "reviewed" and isinstance(
                    value.get("id"), int
                ):
                    kind, identifier = "review", value["id"]
                _add_record(
                    records,
                    _record(
                        kind,
                        str(identifier),
                        value,
                        path,
                        snapshot,
                        timeline_order,
                    ),
                )
                timeline_order += 1
    if object_kind == "pull-request":
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
                        _record(
                            "review",
                            str(value["id"]),
                            value,
                            path,
                            snapshot,
                        ),
                    )
        for path in sorted(
            name for name in snapshot.evidence if name.startswith("review-comments.")
        ):
            for value in _list(snapshot, path):
                node_id = value.get("node_id")
                if not isinstance(node_id, str):
                    raise ArchiveError("GitHub review-comment evidence was invalid")
                _add_record(
                    records,
                    _record("inline-comment", node_id, value, path, snapshot),
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
                    _record(
                        "review-thread",
                        thread_id,
                        thread,
                        path,
                        snapshot,
                    ),
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
            if not all(isinstance(comment, dict) for comment in nodes):
                raise ArchiveError("GitHub review-thread comments were invalid")
            paged_thread_comments.append((thread_id, nodes))
            for comment in nodes:
                if not isinstance(comment.get("id"), str):
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
                    "limitations": ["does_not_establish_fix_or_commit"],
                    "representations": [
                        {
                            "evidence_path": "pull-request.diff",
                            "run_id": snapshot.run["run_id"],
                            "value": snapshot.evidence["pull-request.diff"],
                        }
                    ],
                },
            )

    for thread_id, nodes in paged_thread_comments:
        record = records.get(("review-thread", thread_id))
        if record is None:
            continue
        for representation in reversed(record["representations"]):
            value = representation.get("value")
            if isinstance(value, dict):
                _merge_thread_comments(value, nodes)
            break
    ordered = tuple(
        sorted(
            (
                record
                for record in records.values()
                if record.get("kind") != "ordinary-comment-alias"
            ),
            key=lambda record: _record_sort_key(record, start, end),
        )
    )
    selected = [
        record for record in ordered if "in_range_work" in _roles(record, start, end)
    ]
    return GitHubProjection(
        bool(selected),
        ordered,
        tuple(
            {"kind": kind, "from_native_id": left, "to_native_id": right}
            for kind, left, right in sorted(relations)
        ),
        repository,
        number,
        title,
        tracked_login,
    )


def _github_path(projection: GitHubProjection) -> str:
    owner, _, repository = projection.repository.partition("/")
    kind = (
        "pull"
        if any(record.get("kind") == "pull-request" for record in projection.records)
        else "issue"
    )
    return (
        f"github/{safe_path_component(owner)}/{safe_path_component(repository)}"
        f"/{kind}/{projection.number}"
    )


class GitHubContextAdapter:
    source_kind = "github"
    object_kinds = frozenset({"issue", "pull-request"})
    attribution_policy = ACTOR_SCOPED_ATTRIBUTION_POLICY
    evidence_policy = PROVIDER_EVIDENCE_POLICY

    def project(
        self, snapshot: PublishedSnapshot, start: datetime, end: datetime
    ) -> GitHubProjection:
        return project_github(snapshot, start, end)

    def include(self, projection: object) -> bool:
        if not isinstance(projection, GitHubProjection):
            raise ArchiveError("GitHub context projection was invalid")
        return projection.selected

    def prepare(
        self,
        items: tuple[ContextItem, ...],
        runs: tuple[PublishedRun, ...],
        archive_root: str | Path | None,
    ) -> tuple[ContextItem, ...]:
        scope_ids = sorted(
            {
                item.snapshot.run["source"]["scope_id"]
                for item in items
                if isinstance(item.projection, GitHubProjection)
            }
        )
        if not scope_ids:
            return items
        if archive_root is None:
            raise ArchiveError(
                "GitHub identity profiles require an archive path during "
                "Context extraction"
            )
        historical_logins: dict[str, set[str]] = {
            scope_id: set() for scope_id in scope_ids
        }
        for run in runs:
            source = run.manifest.get("source")
            if not isinstance(source, dict) or source.get("kind") != "github":
                continue
            scope_id = source.get("scope_id")
            if not isinstance(scope_id, str) or scope_id not in historical_logins:
                continue
            login = (
                run.manifest.get("collector", {})
                .get("effective_options", {})
                .get("actor_login")
            )
            if isinstance(login, str) and login:
                historical_logins[scope_id].add(login)
        identities = {
            scope_id: require_github_identity(
                archive_root, scope_id, historical_logins[scope_id]
            )
            for scope_id in scope_ids
        }
        prepared: list[ContextItem] = []
        for item in items:
            projection = item.projection
            if not isinstance(projection, GitHubProjection):
                raise ArchiveError("GitHub context projection was invalid")
            prepared.append(
                replace(
                    item,
                    projection=replace(
                        projection,
                        tracked_identity=identities[
                            item.snapshot.run["source"]["scope_id"]
                        ],
                    ),
                    path=_github_path(projection),
                )
            )
        return tuple(prepared)

    def render(
        self, item: ContextItem, result: ContextExtractionResult, output: Path
    ) -> RenderedContextItem:
        from .context_render import write_markdown  # noqa: PLC0415
        from .github_context_render import render_github  # noqa: PLC0415

        files = render_github(item, result)
        for name, content in files.items():
            path = output / name
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                write_markdown(path, content)
        return rendered_context_item(self, item, result, files)

    def ordering_metadata(
        self, item: ContextItem, _result: ContextExtractionResult
    ) -> ContextOrdering:
        projection = item.projection
        if not isinstance(projection, GitHubProjection):
            raise ArchiveError("GitHub context projection was invalid")
        return ContextOrdering(
            projection.repository,
            (
                item.snapshot.run["source"]["scope_id"],
                item.snapshot.manifest["object_kind"],
                item.snapshot.manifest["source_id"],
            ),
        )


GITHUB_CONTEXT_ADAPTER = GitHubContextAdapter()
