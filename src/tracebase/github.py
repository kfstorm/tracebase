"""GitHub Artifact discovery and source-native evidence hydration."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from .archive import ArchiveError, CollectionRange, CollectionRun, Snapshot
from .collector import CollectionContext, CollectionResult
from .progress import ProgressEvent, ProgressReporter


@dataclass(frozen=True, slots=True)
class GitHubContext(CollectionContext):
    """Resolved GitHub actor context used by the GitHub collector."""

    actor_login: str

    def __post_init__(self) -> None:
        if self.effective_options.get("actor_login") != self.actor_login:
            raise ArchiveError("GitHub context actor login is inconsistent")


_API_ACCEPT = "application/vnd.github+json"
_TIMELINE_ACCEPT = "application/vnd.github+json"
_DIFF_ACCEPT = "application/vnd.github.diff"
_SUCCESS_STATUS_LOWER = 200
_SUCCESS_STATUS_UPPER = 300
_SEARCH_RESULT_LIMIT = 1000
_REQUEST_TIMEOUT_SECONDS = 60
_API_VERSION = "2022-11-28"
_MINIMUM_PARTITION = timedelta(seconds=1)
_DISCOVERY_MATRIX_VERSION = 1


@dataclass(frozen=True)
class _Response:
    body: bytes
    status: int
    headers: dict[str, str]
    observed_at: str = ""


@dataclass(frozen=True)
class _DiscoveryEntry:
    eligibility: str
    query: str
    partition_from: str
    partition_to: str


@dataclass(frozen=True)
class _Candidate:
    source_id: str
    repository: str
    number: int
    object_kind: str
    discovery_entries: tuple[_DiscoveryEntry, ...]


@dataclass(frozen=True)
class _Evidence:
    path: str
    endpoint: str
    accept: str
    response: _Response
    request_metadata: dict[str, Any] | None = None


class _GitHub:
    def __init__(self) -> None:
        self._supports_allow_escape_sequences: bool | None = None

    def _supports_escape_sequences(self) -> bool:
        if self._supports_allow_escape_sequences is not None:
            return self._supports_allow_escape_sequences
        try:
            completed = subprocess.run(
                ["gh", "api", "--help"],
                capture_output=True,
                check=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError("GitHub CLI capability probe failed") from error
        if completed.returncode != 0:
            raise ArchiveError("GitHub CLI capability probe failed")
        self._supports_allow_escape_sequences = (
            b"--allow-escape-sequences" in completed.stdout
        )
        return self._supports_allow_escape_sequences

    def request(self, endpoint: str, accept: str = _API_ACCEPT) -> _Response:
        arguments = [
            "gh",
            "api",
            "--include",
        ]
        if accept == _DIFF_ACCEPT and self._supports_escape_sequences():
            arguments.append("--allow-escape-sequences")
        arguments.extend(
            [
                "-H",
                f"Accept: {accept}",
                "-H",
                f"X-GitHub-Api-Version: {_API_VERSION}",
                endpoint,
            ]
        )
        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                check=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError("GitHub request failed") from error
        if completed.returncode != 0:
            raise ArchiveError("GitHub request failed")
        return self._parse_response(completed.stdout, _observed_at())

    def graphql(self, query: str) -> _Response:
        arguments = [
            "gh",
            "api",
            "graphql",
            "--include",
            "-H",
            f"Accept: {_API_ACCEPT}",
            "-H",
            f"X-GitHub-Api-Version: {_API_VERSION}",
            "-f",
            f"query={query}",
        ]
        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                check=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError("GitHub GraphQL request failed") from error
        if completed.returncode != 0:
            raise ArchiveError("GitHub GraphQL request failed")
        return self._parse_response(completed.stdout, _observed_at())

    @staticmethod
    def _parse_response(raw: bytes, observed_at: str = "") -> _Response:
        separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
        try:
            header_block, body = raw.split(separator, maxsplit=1)
            header_lines = header_block.decode("iso-8859-1").splitlines()
            status = int(header_lines[0].split()[1])
        except IndexError, UnicodeDecodeError, ValueError:
            raise ArchiveError("GitHub response was incomplete") from None
        headers: dict[str, str] = {}
        for line in header_lines[1:]:
            if ":" in line:
                key, value = line.split(":", maxsplit=1)
                headers[key.lower()] = value.strip()
        if not _SUCCESS_STATUS_LOWER <= status < _SUCCESS_STATUS_UPPER:
            raise ArchiveError("GitHub request failed")
        return _Response(
            body=body, status=status, headers=headers, observed_at=observed_at
        )

    @staticmethod
    def json(response: _Response) -> dict[str, Any] | list[Any]:
        try:
            value = json.loads(response.body)
        except json.JSONDecodeError:
            raise ArchiveError("GitHub response was invalid") from None
        if not isinstance(value, (dict, list)):
            raise ArchiveError("GitHub response was invalid")
        return value


def _observed_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _response_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: headers[key]
        for key in ("content-type", "etag", "last-modified", "x-github-media-type")
        if key in headers
    }


def _evidence_file(
    path: str,
    endpoint: str,
    accept: str,
    response: _Response,
    request_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = {
        "endpoint": endpoint,
        "method": "GET",
        "api": "GitHub REST API",
        "api_version": _API_VERSION,
        "accept": accept,
        "response_status": response.status,
        "response_headers": _response_headers(response.headers),
        "observed_at": response.observed_at or _observed_at(),
    }
    if request_metadata is not None:
        request.update(request_metadata)
    return {"path": path, "request": request}


def _page_endpoint(endpoint: str, page: int) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}per_page=100&page={page}"


def _has_next(response: _Response) -> bool:
    link = response.headers.get("link", "")
    return 'rel="next"' in link


def _graphql_data(response: _Response) -> dict[str, Any]:
    value = _GitHub.json(response)
    if not isinstance(value, dict) or value.get("errors"):
        raise ArchiveError("GitHub GraphQL response was invalid")
    data = value.get("data")
    if not isinstance(data, dict):
        raise ArchiveError("GitHub GraphQL response was invalid")
    return data


def _graphql_page_info(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        raise ArchiveError("GitHub GraphQL pagination response was invalid")
    has_next = value.get("hasNextPage")
    cursor = value.get("endCursor")
    if not isinstance(has_next, bool) or (
        cursor is not None and not isinstance(cursor, str)
    ):
        raise ArchiveError("GitHub GraphQL pagination response was invalid")
    if has_next and not cursor:
        raise ArchiveError("GitHub GraphQL pagination response was invalid")
    return has_next, cursor


def _repository_parts(repository: str) -> tuple[str, str]:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ArchiveError("GitHub repository identifier was invalid")
    return owner, name


_REVIEW_COMMENT_FIELDS = """
              nodes {
                id
                databaseId
                body
                path
                line
                originalLine
                startLine
                originalStartLine
                diffSide
                startDiffSide
                diffHunk
                createdAt
                updatedAt
                author {
                  id
                  login
                }
              }
              pageInfo {
                hasNextPage
                endCursor
              }
"""


def _review_threads_query(repository: str, number: int, cursor: str | None) -> str:
    owner, name = _repository_parts(repository)
    after = "null" if cursor is None else json.dumps(cursor)
    return f"""query PullRequestReviewThreads {{
  repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
    pullRequest(number: {number}) {{
      reviewThreads(first: 100, after: {after}) {{
        nodes {{
          id
          path
          line
          originalLine
          startLine
          originalStartLine
          diffSide
          startDiffSide
          isResolved
          isOutdated
          resolvedBy {{
            id
            login
          }}
          comments(first: 100) {{{_REVIEW_COMMENT_FIELDS}          }}
        }}
        pageInfo {{
          hasNextPage
          endCursor
        }}
      }}
    }}
  }}
}}"""


def _review_thread_comments_query(thread_id: str, cursor: str) -> str:
    cursor_value = json.dumps(cursor)
    return f"""query PullRequestReviewThreadComments {{
  node(id: {json.dumps(thread_id)}) {{
    ... on PullRequestReviewThread {{
      id
      comments(first: 100, after: {cursor_value}) {{{_REVIEW_COMMENT_FIELDS}      }}
    }}
  }}
}}"""


def _review_threads_page(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repository = data.get("repository")
    pull_request = (
        repository.get("pullRequest") if isinstance(repository, dict) else None
    )
    connection = (
        pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
    )
    if not isinstance(connection, dict):
        raise ArchiveError("GitHub review-thread response was invalid")
    nodes = connection.get("nodes")
    page_info = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise ArchiveError("GitHub review-thread response was invalid")
    if not all(isinstance(node, dict) for node in nodes):
        raise ArchiveError("GitHub review-thread response was invalid")
    return nodes, page_info


def _review_thread_comment_page(data: dict[str, Any], thread_id: str) -> dict[str, Any]:
    node = data.get("node")
    connection = node.get("comments") if isinstance(node, dict) else None
    if (
        not isinstance(node, dict)
        or node.get("id") != thread_id
        or not isinstance(connection, dict)
        or not isinstance(connection.get("nodes"), list)
        or not isinstance(connection.get("pageInfo"), dict)
    ):
        raise ArchiveError("GitHub review-thread comments response was invalid")
    return connection


def _review_thread_evidence(github: _GitHub, candidate: _Candidate) -> list[_Evidence]:
    evidence: list[_Evidence] = []
    thread_cursor: str | None = None
    thread_page_number = 0
    thread_number = 0
    while True:
        query = _review_threads_query(
            candidate.repository, candidate.number, thread_cursor
        )
        response = github.graphql(query)
        nodes, page_info = _review_threads_page(_graphql_data(response))
        thread_page_number += 1
        evidence.append(
            _Evidence(
                f"review-threads.{thread_page_number:03d}.json",
                "/graphql",
                _API_ACCEPT,
                response,
                {"method": "POST", "api": "GitHub GraphQL API", "query": query},
            )
        )
        for node in nodes:
            thread_id = node.get("id")
            comments = node.get("comments")
            if (
                not isinstance(thread_id, str)
                or not isinstance(comments, dict)
                or not isinstance(comments.get("nodes"), list)
                or not isinstance(comments.get("pageInfo"), dict)
            ):
                raise ArchiveError("GitHub review-thread response was invalid")
            if not all(isinstance(comment, dict) for comment in comments["nodes"]):
                raise ArchiveError("GitHub review-thread response was invalid")
            thread_number += 1
            comment_cursor: str | None = None
            comment_page_number = 1
            has_next_comments, comment_cursor = _graphql_page_info(comments["pageInfo"])
            while has_next_comments:
                if comment_cursor is None:
                    raise ArchiveError(
                        "GitHub review-thread comments pagination was invalid"
                    )
                comment_query = _review_thread_comments_query(thread_id, comment_cursor)
                comment_response = github.graphql(comment_query)
                comment_connection = _review_thread_comment_page(
                    _graphql_data(comment_response), thread_id
                )
                comment_page_number += 1
                evidence.append(
                    _Evidence(
                        f"review-thread-comments.{thread_number:03d}."
                        f"{comment_page_number:03d}.json",
                        "/graphql",
                        _API_ACCEPT,
                        comment_response,
                        {
                            "method": "POST",
                            "api": "GitHub GraphQL API",
                            "query": comment_query,
                        },
                    )
                )
                if not all(
                    isinstance(comment, dict) for comment in comment_connection["nodes"]
                ):
                    raise ArchiveError(
                        "GitHub review-thread comments response was invalid"
                    )
                has_next_comments, comment_cursor = _graphql_page_info(
                    comment_connection["pageInfo"]
                )
        has_next_threads, thread_cursor = _graphql_page_info(page_info)
        if not has_next_threads:
            return evidence


def _pages(
    github: _GitHub, endpoint: str, accept: str = _API_ACCEPT
) -> Iterable[tuple[str, _Response]]:
    page = 1
    while True:
        page_endpoint = _page_endpoint(endpoint, page)
        response = github.request(page_endpoint, accept)
        value = github.json(response)
        if not isinstance(value, list):
            raise ArchiveError("GitHub pagination response was invalid")
        yield page_endpoint, response
        if not _has_next(response):
            return
        page += 1


def _actor(github: _GitHub) -> tuple[str, str]:
    response = github.request("/user")
    value = github.json(response)
    if not isinstance(value, dict):
        raise ArchiveError("GitHub actor response was invalid")
    node_id, login = value.get("node_id"), value.get("login")
    if not isinstance(node_id, str) or not isinstance(login, str):
        raise ArchiveError("GitHub actor response was invalid")
    return node_id, login


def resolve_context() -> GitHubContext:
    """Resolve the authenticated GitHub actor for a Collection Run."""

    actor_id, login = _actor(_GitHub())
    return GitHubContext(
        source_kind="github",
        scope_id=actor_id,
        collector_version="0.1.0",
        effective_options={"actor_login": login},
        actor_login=login,
    )


def _discovery_queries(
    login: str, collection_range: CollectionRange
) -> list[tuple[str, str]]:
    updated = _updated_range(collection_range.start, collection_range.end)
    return [
        ("authorship", f"author:{login} {updated}"),
        ("ordinary_comment", f"commenter:{login} {updated}"),
        ("submitted_review", f"is:pr reviewed-by:{login} {updated}"),
    ]


def _search_response(value: dict[str, Any] | list[Any]) -> tuple[int, list[Any]]:
    if not isinstance(value, dict):
        raise ArchiveError("GitHub discovery response was invalid")
    total_count, items = value.get("total_count"), value.get("items")
    if not isinstance(total_count, int) or not isinstance(items, list):
        raise ArchiveError("GitHub discovery response was invalid")
    if value.get("incomplete_results") is True:
        raise ArchiveError("GitHub discovery was incomplete")
    return total_count, items


def _discover(  # noqa: PLR0915
    github: _GitHub,
    login: str,
    collection_range: CollectionRange,
    reporter: ProgressReporter,
) -> tuple[dict[str, _Candidate], list[dict[str, Any]]]:
    candidates: dict[str, _Candidate] = {}
    coverage_queries: list[dict[str, Any]] = []
    pages_processed = 0
    for name, base_query in _discovery_queries(login, collection_range):
        partitions = [(collection_range.start, collection_range.end)]
        while partitions:
            start, end = partitions.pop(0)
            start_text = _timestamp(start)
            end_text = _timestamp(end)
            original_range = _updated_range(
                collection_range.start, collection_range.end
            )
            query = base_query.replace(
                original_range,
                _updated_range(start, end),
            )
            discovery_entry = _DiscoveryEntry(name, query, start_text, end_text)
            endpoint = "/search/issues?" + urlencode(
                {"q": query, "sort": "updated", "order": "asc"}
            )
            response = github.request(_page_endpoint(endpoint, 1))
            total_count, items = _search_response(github.json(response))
            if total_count > _SEARCH_RESULT_LIMIT:
                if end - start <= _MINIMUM_PARTITION:
                    raise ArchiveError(
                        "GitHub discovery exceeded the 1,000 result limit"
                    )
                midpoint = start + timedelta(
                    seconds=int((end - start).total_seconds()) // 2
                )
                coverage_queries.append(
                    {
                        "reason": name,
                        "partition": {"from": start_text, "to": end_text},
                        "query": query,
                        "result_count": total_count,
                        "result_source_ids": _source_ids(items),
                        "pages": 1,
                        "pagination_complete": False,
                        "disposition": "split",
                    }
                )
                partitions[0:0] = [(start, midpoint), (midpoint, end)]
                reporter.emit(
                    ProgressEvent(
                        kind="update",
                        task_id="github.discover",
                        completed=pages_processed,
                        message=f"{name}: partitioned page 1 ({total_count} results)",
                    )
                )
                continue
            page = 1
            result_count = 0
            result_source_ids: list[str] = []
            while True:
                if page > 1:
                    response = github.request(_page_endpoint(endpoint, page))
                total_count, items = _search_response(github.json(response))
                if total_count > _SEARCH_RESULT_LIMIT:
                    raise ArchiveError(
                        "GitHub discovery exceeded the 1,000 result limit"
                    )
                for item in items:
                    if not isinstance(item, dict):
                        raise ArchiveError("GitHub discovery response was invalid")
                    source_id = item.get("node_id")
                    repository = item.get("repository_url")
                    number = item.get("number")
                    if (
                        not isinstance(source_id, str)
                        or not isinstance(repository, str)
                        or not isinstance(number, int)
                    ):
                        raise ArchiveError("GitHub discovery response was invalid")
                    repository = repository.removeprefix(
                        "https://api.github.com/repos/"
                    )
                    object_kind = "pull-request" if "pull_request" in item else "issue"
                    prior = candidates.get(source_id)
                    if prior is None:
                        candidates[source_id] = _Candidate(
                            source_id,
                            repository,
                            number,
                            object_kind,
                            (discovery_entry,),
                        )
                    elif (prior.repository, prior.number, prior.object_kind) == (
                        repository,
                        number,
                        object_kind,
                    ):
                        candidates[source_id] = _Candidate(
                            source_id,
                            repository,
                            number,
                            object_kind,
                            tuple(
                                dict.fromkeys(
                                    (*prior.discovery_entries, discovery_entry)
                                )
                            ),
                        )
                    else:
                        raise ArchiveError(
                            "GitHub discovery returned inconsistent Artifact IDs"
                        )
                    result_count += 1
                    if source_id not in result_source_ids:
                        result_source_ids.append(source_id)
                pages_processed += 1
                reporter.emit(
                    ProgressEvent(
                        kind="update",
                        task_id="github.discover",
                        completed=pages_processed,
                        message=f"{name}: page {page}, {len(candidates)} candidates",
                    )
                )
                if not _has_next(response):
                    if total_count > len(result_source_ids):
                        raise ArchiveError("GitHub discovery pagination incomplete")
                    coverage_queries.append(
                        {
                            "reason": name,
                            "partition": {"from": start_text, "to": end_text},
                            "query": query,
                            "pages": page,
                            "result_count": result_count,
                            "result_source_ids": result_source_ids,
                            "pagination_complete": True,
                            "disposition": "complete",
                        }
                    )
                    break
                page += 1
    return candidates, coverage_queries


def _source_ids(items: list[Any]) -> list[str]:
    source_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("node_id"), str):
            raise ArchiveError("GitHub discovery response was invalid")
        source_ids.append(item["node_id"])
    return source_ids


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _updated_range(start: datetime, end: datetime) -> str:
    # GitHub Search's `a..b` range is inclusive at both ends, while
    # Tracebase Collection Ranges are half-open [start, end). Since
    # Collection Range endpoints are restricted to whole seconds,
    # subtracting one second from `end` gives an exact, gap-free,
    # non-overlapping representation of the Tracebase interval.
    inclusive_end = end - timedelta(seconds=1)
    return f"updated:{_timestamp(start)}..{_timestamp(inclusive_end)}"


def _hydrate(
    run: CollectionRun,
    github: _GitHub,
    candidate: _Candidate,
) -> None:
    observed_from = _observed_at()
    base = f"/repos/{candidate.repository}"
    issue_endpoint = f"{base}/issues/{candidate.number}"
    issue_response = github.request(issue_endpoint)
    issue = github.json(issue_response)
    if not isinstance(issue, dict) or issue.get("node_id") != candidate.source_id:
        raise ArchiveError("GitHub Artifact payload was invalid")
    if ("pull_request" in issue) != (candidate.object_kind == "pull-request"):
        raise ArchiveError("GitHub Artifact kind changed during hydration")
    evidence: list[_Evidence] = [
        _Evidence("issue.json", issue_endpoint, _API_ACCEPT, issue_response)
    ]
    comments_endpoint = f"{issue_endpoint}/comments"
    for page_number, (page, response) in enumerate(
        _pages(github, comments_endpoint), start=1
    ):
        _as_list(github.json(response))
        evidence.append(
            _Evidence(f"comments.{page_number:03d}.json", page, _API_ACCEPT, response)
        )
    timeline_endpoint = f"{issue_endpoint}/timeline"
    for page_number, (page, response) in enumerate(
        _pages(github, timeline_endpoint, _TIMELINE_ACCEPT), start=1
    ):
        evidence.append(
            _Evidence(
                f"timeline.{page_number:03d}.json",
                page,
                _TIMELINE_ACCEPT,
                response,
            )
        )
    if candidate.object_kind == "pull-request":
        pull_endpoint = f"{base}/pulls/{candidate.number}"
        pull_response = github.request(pull_endpoint)
        pull = github.json(pull_response)
        if not isinstance(pull, dict) or pull.get("node_id") != candidate.source_id:
            raise ArchiveError("GitHub Pull Request payload was invalid")
        evidence.append(
            _Evidence("pull-request.json", pull_endpoint, _API_ACCEPT, pull_response)
        )
        reviews_endpoint = f"{pull_endpoint}/reviews"
        for page_number, (page, response) in enumerate(
            _pages(github, reviews_endpoint), start=1
        ):
            _as_list(github.json(response))
            evidence.append(
                _Evidence(
                    f"reviews.{page_number:03d}.json", page, _API_ACCEPT, response
                )
            )
        review_comments_endpoint = f"{pull_endpoint}/comments"
        for page_number, (page, response) in enumerate(
            _pages(github, review_comments_endpoint), start=1
        ):
            evidence.append(
                _Evidence(
                    f"review-comments.{page_number:03d}.json",
                    page,
                    _API_ACCEPT,
                    response,
                )
            )
        evidence.extend(_review_thread_evidence(github, candidate))
        diff_response = github.request(pull_endpoint, _DIFF_ACCEPT)
        evidence.append(
            _Evidence("pull-request.diff", pull_endpoint, _DIFF_ACCEPT, diff_response)
        )
    selected_entries = list(candidate.discovery_entries)
    selected = sorted({entry.eligibility for entry in selected_entries})
    provenance = (
        {
            "eligibility": selected,
            "discovery_query_entries": [
                {
                    "reason": entry.eligibility,
                    "query": entry.query,
                    "partition": {
                        "from": entry.partition_from,
                        "to": entry.partition_to,
                    },
                }
                for entry in selected_entries
            ],
        },
    )
    snapshot = Snapshot(
        source_kind="github",
        object_kind=candidate.object_kind,
        source_id=candidate.source_id,
        observation_window={"from": observed_from, "to": _observed_at()},
        evidence_files=tuple(
            _evidence_file(
                item.path,
                item.endpoint,
                item.accept,
                item.response,
                item.request_metadata,
            )
            for item in evidence
        ),
        selection_provenance=provenance,
    )
    snapshot_root = run.write_snapshot(snapshot)
    for item in evidence:
        run.write_evidence(snapshot_root, item.path, item.response.body)


def _as_list(value: dict[str, Any] | list[Any]) -> list[Any]:
    if not isinstance(value, list):
        raise ArchiveError("GitHub pagination response was invalid")
    return value


def collect(
    run: CollectionRun,
    reporter: ProgressReporter,
) -> CollectionResult:
    """Discover and hydrate all eligible Artifacts."""

    github = _GitHub()
    actor_id = run.scope_id
    login = run.effective_options.get("actor_login")
    if not isinstance(login, str):
        raise ArchiveError("GitHub Collection Run actor login is invalid")
    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="github.discover",
            label="Discovering GitHub artifacts",
        )
    )
    candidates, discovery = _discover(github, login, run.collection_range, reporter)
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="github.discover",
            message=f"{len(candidates)} candidates",
        )
    )
    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="github.hydrate",
            label="Hydrating GitHub artifacts",
            total=len(candidates),
        )
    )
    selected = 0
    for candidate in candidates.values():
        current = f"{candidate.repository}#{candidate.number}"
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="github.hydrate",
                completed=selected,
                total=len(candidates),
                current=current,
            )
        )
        _hydrate(run, github, candidate)
        selected += 1
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="github.hydrate",
                completed=selected,
                total=len(candidates),
                current=current,
            )
        )
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="github.hydrate",
            completed=selected,
            message=f"{selected} artifacts",
        )
    )
    return CollectionResult(
        coverage={
            "actor": {"node_id": actor_id, "login": login},
            "discovery_matrix_version": _DISCOVERY_MATRIX_VERSION,
            "queries": discovery,
            "permission_boundary": (
                "responses visible to the authenticated GitHub actor"
            ),
            "pagination_complete": True,
            "selected_artifacts": selected,
        }
    )
