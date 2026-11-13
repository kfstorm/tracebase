"""Pending review comments remain raw evidence, not submitted Context activity."""

import json
from pathlib import Path

import pytest

from tests.test_context_contracts import (
    github_base,
    publish_github,
    request,
    text,
    write_github_profile,
)
from tracebase.archive import Archive
from tracebase.context import extract_context, generate_context, load_archive
from tracebase.github_context import GitHubProjection, github_user_work_record_ids


def thread_evidence(
    first_page: list[dict[str, object]], next_page: list[dict[str, object]] | None
) -> dict[str, object]:
    comments = {
        "nodes": first_page,
        "pageInfo": {
            "hasNextPage": next_page is not None,
            "endCursor": "comment-cursor" if next_page is not None else None,
        },
    }
    evidence: dict[str, object] = {
        "review-threads.001.json": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"id": "THREAD_SYNTHETIC", "comments": comments}]
                        }
                    }
                }
            }
        }
    }
    if next_page is not None:
        evidence["review-thread-comments.001.002.json"] = {
            "data": {
                "node": {
                    "id": "THREAD_SYNTHETIC",
                    "comments": {
                        "nodes": next_page,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    return evidence


def publish_pending_archive(
    tmp_path: Path, evidence: dict[str, object]
) -> tuple[Archive, dict[Path, bytes]]:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": github_base(),
            "pull-request.json": {"node_id": "PR_1"},
            **evidence,
        },
        effective_options={"actor_login": "tracked-user"},
    )
    write_github_profile(archive)
    for name, value in evidence.items():
        assert next(archive.root.rglob(name)).read_bytes() == json.dumps(value).encode()
    return archive, archive_bytes(archive)


def archive_bytes(archive: Archive) -> dict[Path, bytes]:
    return {
        path.relative_to(archive.root): path.read_bytes()
        for path in archive.root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("paginated", [False, True], ids=["first-page", "next-page"])
@pytest.mark.parametrize(
    "state", [None, "SUBMITTED"], ids=["legacy-id-only", "submitted"]
)
@pytest.mark.parametrize("background", [False, True], ids=["activity", "background"])
def test_pending_comments_do_not_render_in_mixed_review_threads(
    tmp_path: Path, paginated: bool, state: str | None, background: bool
) -> None:
    submitted: list[dict[str, object]] = [
        {"id": "COMMENT_TRACKED"},
        {"id": "COMMENT_COLLABORATOR"},
    ]
    if state is not None:
        for node in submitted:
            node["state"] = state
    pending: dict[str, object] = {"id": "COMMENT_PENDING", "state": "PENDING"}
    evidence = thread_evidence(
        submitted[:1] if paginated else [submitted[0], pending, submitted[1]],
        [pending, submitted[1]] if paginated else None,
    )
    date = "2025-12-31" if background else "2026-01-01"
    evidence["review-comments.001.json"] = [
        {
            "id": index,
            "node_id": node["id"],
            "body": f"Submitted evidence from {login}",
            "user": {"login": login},
            "path": "src/example.py",
            "line": 12,
            "created_at": f"{date}T01:00:00Z",
            "pull_request_review_id": 7,
        }
        for index, (node, login) in enumerate(
            zip(submitted, ["tracked-user", "collaborator"], strict=True), start=1
        )
    ]
    archive, original = publish_pending_archive(tmp_path, evidence)
    context_request = request()
    result = extract_context(context_request, load_archive(archive.root), archive.root)
    projection = result.items[0].projection
    assert isinstance(projection, GitHubProjection)
    user_work = github_user_work_record_ids(
        projection, context_request.start, context_request.end
    )
    assert user_work == (
        frozenset()
        if background
        else frozenset({("inline-comment", "COMMENT_TRACKED")})
    )
    for node in submitted:
        assert {
            "kind": "thread-inline-comment",
            "from_native_id": "THREAD_SYNTHETIC",
            "to_native_id": node["id"],
        } in projection.relations

    output = tmp_path / "output"
    generate_context(archive.root, context_request, output)
    markdown = text(output, "background.md" if background else "activity.md")
    assert markdown.count("Review thread · src/example.py:12") == 1
    assert markdown.index("Submitted evidence from tracked-user") < markdown.index(
        "Submitted evidence from collaborator"
    )
    for login, attribution in (
        (
            "tracked-user (tracked account)",
            "Context only" if background else "User work",
        ),
        ("collaborator", "Context only"),
    ):
        assert f"**@{login} · {date} 09:00 · [{attribution}]**" in markdown
    assert markdown.count("Submitted evidence from") == 2
    assert markdown.count("**@") == 2
    all_markdown = "\n".join(path.read_text() for path in output.rglob("*.md"))
    assert "COMMENT_PENDING" not in all_markdown
    assert "PENDING" not in all_markdown
    assert markdown.count("[User work]") == (0 if background else 1)
    assert archive_bytes(archive) == original


@pytest.mark.parametrize("paginated", [False, True], ids=["first-page", "next-page"])
@pytest.mark.parametrize(
    "other_activity", [False, True], ids=["unselected", "selected"]
)
@pytest.mark.parametrize("timestamp_key", ["createdAt", "created_at"])
def test_pending_only_threads_do_not_create_activity_or_user_work(
    tmp_path: Path, paginated: bool, other_activity: bool, timestamp_key: str
) -> None:
    pending: dict[str, object] = {
        "id": "COMMENT_PENDING",
        "state": "PENDING",
        "body": "Unsubmitted draft evidence",
        "author": {"login": "tracked-user"},
        timestamp_key: "2026-01-01T01:00:00Z",
        "publishedAt": None,
    }
    evidence = thread_evidence(
        [] if paginated else [pending], [pending] if paginated else None
    )
    evidence["issue.json"] = {
        **github_base(),
        "created_at": "2025-12-01T00:00:00Z",
        "updated_at": "2025-12-01T00:00:00Z",
    }
    if other_activity:
        evidence["comments.001.json"] = [
            {
                "id": 1,
                "body": "Submitted ordinary comment",
                "user": {"login": "collaborator"},
                "created_at": "2026-01-01T02:00:00Z",
            }
        ]
    archive, original = publish_pending_archive(tmp_path, evidence)
    context_request = request()
    result = extract_context(context_request, load_archive(archive.root), archive.root)
    assert len(result.items) == int(other_activity)
    if other_activity:
        projection = result.items[0].projection
        assert isinstance(projection, GitHubProjection)
        assert not github_user_work_record_ids(
            projection, context_request.start, context_request.end
        )
    output = tmp_path / "output"
    generate_context(archive.root, context_request, output)
    markdown = "\n".join(path.read_text() for path in output.rglob("*.md"))
    assert "Unsubmitted draft evidence" not in markdown
    assert "COMMENT_PENDING" not in markdown
    assert "Review thread" not in markdown
    if other_activity:
        activity = text(output, "activity.md")
        assert "Submitted ordinary comment" in activity
        assert "[User work]" not in activity
        assert not list(output.rglob("background.md"))
    assert archive_bytes(archive) == original
