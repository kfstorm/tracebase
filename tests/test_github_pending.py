"""Draft review comments do not require submitted REST comment evidence."""

import json
from pathlib import Path

import pytest

from tests.test_cli import (
    build_github_pr_fixture,
    build_github_request,
    build_github_run,
    build_test_reporter,
)
from tracebase.archive import Archive
from tracebase.github import _Response, collect


@pytest.mark.parametrize("paginated", [False, True])
def test_collection_ignores_pending_review_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paginated: bool
) -> None:
    item, responses = build_github_pr_fixture()
    rest_endpoint = "/repos/octo/example/pulls/7/comments?per_page=100&page=1"
    rest_body = responses[rest_endpoint][0]
    published_comments = [
        {"id": comment["node_id"], "state": "SUBMITTED"}
        for comment in json.loads(rest_body)
    ]
    pending_comment = {"id": "synthetic-draft-comment", "state": "PENDING"}
    comments = {
        "nodes": published_comments + ([] if paginated else [pending_comment]),
        "pageInfo": {
            "hasNextPage": paginated,
            "endCursor": "synthetic-cursor" if paginated else None,
        },
    }
    thread_body = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"id": "synthetic-thread", "comments": comments}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
    ).encode()
    comment_body = json.dumps(
        {
            "data": {
                "node": {
                    "id": "synthetic-thread",
                    "comments": {
                        "nodes": [pending_comment],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    ).encode()
    base_request = build_github_request(item, responses)
    rest_calls = 0

    def request(
        self: object, endpoint: str, accept: str = "application/vnd.github+json"
    ) -> _Response:
        nonlocal rest_calls
        if endpoint == rest_endpoint:
            rest_calls += 1
        return base_request(self, endpoint, accept)

    queries = []

    def graphql(_self: object, query: str) -> _Response:
        queries.append(query)
        body = (
            comment_body if "PullRequestReviewThreadComments" in query else thread_body
        )
        return _Response(body, 200, {})

    monkeypatch.setattr("tracebase.github._GitHub.request", request)
    monkeypatch.setattr("tracebase.github._GitHub.graphql", graphql)
    run = build_github_run(Archive(tmp_path))
    result = collect(run, build_test_reporter())
    published = run.publish(result.coverage)
    snapshot = next((published / "snapshots/pull-request").iterdir())
    assert run.snapshot_count == 1
    assert rest_calls == 1
    assert all("state" in query for query in queries)
    assert (snapshot / "review-comments.001.json").read_bytes() == rest_body
    assert (snapshot / "review-threads.001.json").read_bytes() == thread_body
    if paginated:
        assert (
            snapshot / "review-thread-comments.001.002.json"
        ).read_bytes() == comment_body
