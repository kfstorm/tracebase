"""GitHub Artifact discovery and source-native evidence hydration."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from .archive import ArchiveError, CollectionRange, CollectionRun, Snapshot

_API_ACCEPT = "application/vnd.github+json"
_TIMELINE_ACCEPT = "application/vnd.github+json"
_DIFF_ACCEPT = "application/vnd.github.diff"
_SUCCESS_STATUS_LOWER = 200
_SUCCESS_STATUS_UPPER = 300
_SEARCH_RESULT_LIMIT = 1000
_REQUEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class _Response:
    body: bytes
    status: int
    headers: dict[str, str]


@dataclass(frozen=True)
class _Candidate:
    source_id: str
    repository: str
    number: int
    object_kind: str
    queries: frozenset[str]


@dataclass(frozen=True)
class _Evidence:
    path: str
    endpoint: str
    accept: str
    response: _Response


class _GitHub:
    def request(self, endpoint: str, accept: str = _API_ACCEPT) -> _Response:
        try:
            completed = subprocess.run(
                ["gh", "api", "--include", "-H", f"Accept: {accept}", endpoint],
                capture_output=True,
                check=False,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError("GitHub request failed") from error
        if completed.returncode != 0:
            raise ArchiveError("GitHub request failed")
        return self._parse_response(completed.stdout)

    @staticmethod
    def _parse_response(raw: bytes) -> _Response:
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
        return _Response(body=body, status=status, headers=headers)

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
    path: str, endpoint: str, accept: str, response: _Response
) -> dict[str, Any]:
    return {
        "path": path,
        "request": {
            "endpoint": endpoint,
            "api": "GitHub REST API",
            "accept": accept,
            "response_status": response.status,
            "response_headers": _response_headers(response.headers),
            "observed_at": _observed_at(),
        },
    }


def _page_endpoint(endpoint: str, page: int) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}per_page=100&page={page}"


def _has_next(response: _Response) -> bool:
    link = response.headers.get("link", "")
    return 'rel="next"' in link


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


def _discovery_queries(
    login: str, collection_range: CollectionRange
) -> list[tuple[str, str]]:
    updated = (
        f"updated:>={collection_range.from_text} updated:<{collection_range.to_text}"
    )
    return [
        ("authored", f"author:{login} {updated}"),
        ("ordinary-commented", f"commenter:{login} {updated}"),
        ("submitted-reviewed", f"is:pr reviewed-by:{login} {updated}"),
    ]


def _discover(
    github: _GitHub, login: str, collection_range: CollectionRange
) -> tuple[dict[str, _Candidate], list[dict[str, Any]]]:
    candidates: dict[str, _Candidate] = {}
    coverage_queries: list[dict[str, Any]] = []
    for name, query in _discovery_queries(login, collection_range):
        endpoint = "/search/issues?" + urlencode(
            {"q": query, "sort": "updated", "order": "asc"}
        )
        page = 1
        result_count = 0
        while True:
            page_endpoint = _page_endpoint(endpoint, page)
            response = github.request(page_endpoint)
            value = github.json(response)
            if not isinstance(value, dict):
                raise ArchiveError("GitHub discovery response was invalid")
            total_count, items = value.get("total_count"), value.get("items")
            if not isinstance(total_count, int) or not isinstance(items, list):
                raise ArchiveError("GitHub discovery response was invalid")
            if value.get("incomplete_results") is True:
                raise ArchiveError("GitHub discovery was incomplete")
            if total_count > _SEARCH_RESULT_LIMIT:
                raise ArchiveError("GitHub discovery exceeded the 1,000 result limit")
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
                repository = repository.removeprefix("https://api.github.com/repos/")
                if "/" not in repository:
                    raise ArchiveError("GitHub discovery response was invalid")
                object_kind = "pull-request" if "pull_request" in item else "issue"
                prior = candidates.get(source_id)
                if prior is None:
                    candidates[source_id] = _Candidate(
                        source_id, repository, number, object_kind, frozenset({name})
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
                        prior.queries | {name},
                    )
                else:
                    raise ArchiveError(
                        "GitHub discovery returned inconsistent Artifact IDs"
                    )
                result_count += 1
            if not _has_next(response):
                coverage_queries.append(
                    {
                        "name": name,
                        "query": query,
                        "pages": page,
                        "results": result_count,
                        "pagination_complete": True,
                    }
                )
                break
            page += 1
    return candidates, coverage_queries


def _actor_id(value: Any) -> str | None:
    return (
        value.get("node_id")
        if isinstance(value, dict) and isinstance(value.get("node_id"), str)
        else None
    )


def _hydrate(
    run: CollectionRun,
    github: _GitHub,
    candidate: _Candidate,
    actor_id: str,
    queries: dict[str, str],
) -> bool:
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
    comments: list[Any] = []
    comments_endpoint = f"{issue_endpoint}/comments"
    for page_number, (page, response) in enumerate(
        _pages(github, comments_endpoint), start=1
    ):
        comments.extend(_as_list(github.json(response)))
        evidence.append(
            _Evidence(
                f"comments/page-{page_number:03d}.json", page, _API_ACCEPT, response
            )
        )
    timeline_endpoint = f"{issue_endpoint}/timeline"
    for page_number, (page, response) in enumerate(
        _pages(github, timeline_endpoint, _TIMELINE_ACCEPT), start=1
    ):
        evidence.append(
            _Evidence(
                f"timeline/page-{page_number:03d}.json",
                page,
                _TIMELINE_ACCEPT,
                response,
            )
        )
    reviews: list[Any] = []
    if candidate.object_kind == "pull-request":
        pull_endpoint = f"{base}/pulls/{candidate.number}"
        pull_response = github.request(pull_endpoint)
        evidence.append(
            _Evidence("pull-request.json", pull_endpoint, _API_ACCEPT, pull_response)
        )
        reviews_endpoint = f"{pull_endpoint}/reviews"
        for page_number, (page, response) in enumerate(
            _pages(github, reviews_endpoint), start=1
        ):
            reviews.extend(_as_list(github.json(response)))
            evidence.append(
                _Evidence(
                    f"reviews/page-{page_number:03d}.json", page, _API_ACCEPT, response
                )
            )
        review_comments_endpoint = f"{pull_endpoint}/comments"
        for page_number, (page, response) in enumerate(
            _pages(github, review_comments_endpoint), start=1
        ):
            evidence.append(
                _Evidence(
                    f"review-comments/page-{page_number:03d}.json",
                    page,
                    _API_ACCEPT,
                    response,
                )
            )
        diff_response = github.request(pull_endpoint, _DIFF_ACCEPT)
        evidence.append(
            _Evidence("pull-request.diff", pull_endpoint, _DIFF_ACCEPT, diff_response)
        )
    eligibility = []
    if _actor_id(issue.get("user")) == actor_id:
        eligibility.append("authored")
    if any(
        _actor_id(comment.get("user")) == actor_id
        for comment in comments
        if isinstance(comment, dict)
    ):
        eligibility.append("ordinary-commented")
    if any(
        _actor_id(review.get("user")) == actor_id
        and review.get("submitted_at") is not None
        for review in reviews
        if isinstance(review, dict)
    ):
        eligibility.append("submitted-reviewed")
    selected = sorted(set(eligibility) & candidate.queries)
    if not selected:
        return False
    provenance = (
        {
            "eligibility": selected,
            "discovery_query_entries": [
                {"name": name, "query": queries[name]} for name in selected
            ],
        },
    )
    snapshot = Snapshot(
        source_kind="github",
        object_kind=candidate.object_kind,
        source_id=candidate.source_id,
        observation_window={"from": observed_from, "to": _observed_at()},
        evidence_files=tuple(
            _evidence_file(item.path, item.endpoint, item.accept, item.response)
            for item in evidence
        ),
        selection_provenance=provenance,
    )
    snapshot_root = run.write_snapshot(snapshot)
    for item in evidence:
        run.write_evidence(snapshot_root, item.path, item.response.body)
    return True


def _as_list(value: dict[str, Any] | list[Any]) -> list[Any]:
    if not isinstance(value, list):
        raise ArchiveError("GitHub pagination response was invalid")
    return value


def collect(run: CollectionRun, actor: tuple[str, str] | None = None) -> dict[str, Any]:
    """Discover and hydrate all eligible Artifacts, returning observed Coverage."""

    github = _GitHub()
    actor_id, login = actor or _actor(github)
    candidates, discovery = _discover(github, login, run.collection_range)
    queries = dict(_discovery_queries(login, run.collection_range))
    selected = 0
    for candidate in candidates.values():
        selected += _hydrate(run, github, candidate, actor_id, queries)
    return {
        "actor": {"node_id": actor_id, "login": login},
        "discovery_matrix": discovery,
        "permission_boundary": "responses visible to the authenticated GitHub actor",
        "pagination_complete": True,
        "selected_artifacts": selected,
    }
