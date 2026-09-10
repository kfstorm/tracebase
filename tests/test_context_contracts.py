import hashlib
import json
import urllib.request
from pathlib import Path

import pytest

from tracebase.archive import (
    Archive,
    CollectionRange,
    CollectionRun,
    Snapshot,
    encode_path_id,
)
from tracebase.context import (
    ContextError,
    ContextRequest,
    generate_context,
    load_archive,
)


def request() -> ContextRequest:
    return ContextRequest.parse(
        "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00"
    )


def run(
    archive: Archive,
    run_id: str = "run-1",
    source_kind: str = "opencode",
    from_text: str = "2026-01-01T00:00:00+08:00",
    to_text: str = "2026-01-02T00:00:00+08:00",
) -> CollectionRun:
    return CollectionRun(
        archive,
        source_kind,
        "instance-1" if source_kind == "opencode" else "tracked-actor",
        CollectionRange.parse(from_text, to_text),
        "test",
        {},
        run_id=run_id,
    )


def message(
    message_id: str,
    created: str,
    parts: list[dict[str, object]] | None = None,
    role: str = "user",
) -> dict[str, object]:
    return {"id": message_id, "created": created, "role": role, "parts": parts or []}


def session(
    session_id: str,
    *,
    messages: list[dict[str, object]] | None = None,
    parent_id: str | None = None,
    title: str = "Trace work",
    directory: str = "/home/kfstorm/dev/tracebase",
) -> dict[str, object]:
    info: dict[str, object] = {
        "id": session_id,
        "title": title,
        "directory": directory,
    }
    payload: dict[str, object] = {
        "id": session_id,
        "info": info,
        "messages": messages or [],
    }
    if parent_id is not None:
        payload["parentID"] = parent_id
    return payload


def publish_opencode(archive: Archive, payload: dict[str, object], run_id: str) -> None:
    current = run(
        archive,
        run_id,
        from_text=(
            "2026-01-02T00:00:00+08:00"
            if run_id == "child-run"
            else "2026-01-01T00:00:00+08:00"
        ),
        to_text=(
            "2026-01-03T00:00:00+08:00"
            if run_id == "child-run"
            else "2026-01-02T00:00:00+08:00"
        ),
    )
    session_id = str(payload["id"])
    snapshot = current.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            session_id,
            current.collection_range.as_manifest(),
            ({"path": "session.json"},),
            metadata={
                "session": {
                    "id": session_id,
                    "directory": "/home/kfstorm/dev/tracebase",
                }
            },
        )
    )
    current.write_evidence(snapshot, "session.json", json.dumps(payload).encode())
    current.publish({"selected_session_count": 1})


def publish_github(
    archive: Archive,
    source_id: str,
    evidence: dict[str, object | str],
    *,
    object_kind: str = "pull-request",
    run_id: str = "github-run",
    from_text: str = "2026-01-01T00:00:00+08:00",
    to_text: str = "2026-01-02T00:00:00+08:00",
) -> None:
    current = run(archive, run_id, "github", from_text, to_text)
    snapshot = current.write_snapshot(
        Snapshot(
            "github",
            object_kind,
            source_id,
            current.collection_range.as_manifest(),
            tuple({"path": name} for name in evidence),
        )
    )
    for name, value in evidence.items():
        content = value if isinstance(value, str) else json.dumps(value)
        current.write_evidence(snapshot, name, content.encode())
    current.publish({"selected_artifacts": 1, "pagination_complete": True})


def files(output: Path) -> set[str]:
    return {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }


def text(output: Path, suffix: str) -> str:
    path = next(output.rglob(suffix))
    return path.read_text()


def github_base(source_id: str = "PR_1", number: int = 1) -> dict[str, object]:
    return {
        "node_id": source_id,
        "repository_url": "https://api.github.com/repos/kfstorm/tracebase",
        "number": number,
        "title": "Context Output",
        "body": "Description",
        "state": "open",
        "user": {"login": "author"},
        "created_at": "2025-12-31T16:00:00Z",
        "updated_at": "2025-12-31T16:00:00Z",
    }


def activity_evidence() -> dict[str, object]:
    return {
        "comments.001.json": [
            {"id": 1, "body": "activity", "created_at": "2026-01-01T01:00:00Z"}
        ]
    }


def opencode_activity(
    archive: Archive, tmp_path: Path, parts: list[dict[str, object]], created: str
) -> str:
    publish_opencode(
        archive,
        session("root", messages=[message("m", created, parts)]),
        "run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    return text(output, "activity.md")


def github_output(
    archive: Archive, tmp_path: Path, evidence: dict[str, object | str]
) -> Path:
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": github_base(),
            "pull-request.json": {"node_id": "PR_1"},
            **evidence,
        },
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    return output


def test_empty_output_has_only_useful_index_without_front_matter(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    output = tmp_path / "output"
    generate_context(archive, request(), output)
    assert files(output) == {"index.md"}
    index = (output / "index.md").read_text()
    assert "Requested interval" in index
    assert "source_scope_id" not in index
    assert "coverage" not in index.lower()


def test_empty_removed_run_directory_is_tolerated(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    (archive / "runs" / "removed-run" / "snapshots").mkdir(parents=True)

    output = tmp_path / "output"
    generate_context(archive, request(), output)

    assert files(output) == {"index.md"}


def test_nonempty_unregistered_run_content_fails(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    invalid_run = archive / "runs" / "invalid-run"
    (invalid_run / "snapshots").mkdir(parents=True)
    (invalid_run / "unregistered.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ContextError, match="unregistered entries"):
        load_archive(archive)


def test_missing_declared_evidence_fails_before_publication(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    current = run(archive)
    snapshot = current.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "session-1",
            current.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    current.write_evidence(
        snapshot, "session.json", b'{"id":"session-1","messages":[]}'
    )
    current.publish({})
    evidence = (
        archive.root
        / "runs"
        / current.run_id
        / "snapshots"
        / "session"
        / encode_path_id("session-1")
        / "session.json"
    )
    evidence.unlink()

    with pytest.raises(ContextError, match="missing"):
        generate_context(archive.root, request(), tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_github_uses_natural_path_and_heading(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(archive, tmp_path, activity_evidence())
    assert "github/kfstorm/tracebase/pull/1/overview.md" in files(output)
    overview = text(output, "overview.md")
    assert "# kfstorm/tracebase PR #1" in overview
    assert "PR_1" not in overview
    assert "github/kfstorm/tracebase" in (output / "index.md").read_text()


def test_issue_uses_issue_path_and_domain_author_wording(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_github(
        archive,
        "I_1",
        {
            "issue.json": {**github_base("I_1"), "number": 7},
            **activity_evidence(),
        },
        object_kind="issue",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    overview = text(output, "overview.md")
    assert "github/kfstorm/tracebase/issue/7/overview.md" in files(output)
    assert "Author: @author" in overview
    assert "Actor" not in overview


def test_comment_aliases_render_once_and_equal_bodies_remain_distinct(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "comments.001.json": [
            {
                "id": 10,
                "node_id": "IC_10",
                "body": "same",
                "user": {"login": "a"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 11,
                "node_id": "IC_11",
                "body": "same",
                "user": {"login": "b"},
                "created_at": "2026-01-01T02:00:00Z",
            },
        ],
        "timeline.001.json": [
            {
                "id": 10,
                "node_id": "IC_10",
                "event": "commented",
                "body": "same",
                "actor": {"login": "a"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 11,
                "node_id": "IC_11",
                "event": "commented",
                "body": "same",
                "actor": {"login": "b"},
                "created_at": "2026-01-01T02:00:00Z",
            },
        ],
    }
    output = github_output(archive, tmp_path, evidence)
    activity = text(output, "activity.md")
    assert activity.count("same") == 2
    assert activity.count("**@a") == 1
    assert activity.count("**@b") == 1


def test_review_threads_are_materialized_and_unthreaded_comments_remain(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "review-comments.001.json": [
            {
                "id": 20,
                "node_id": "IC_20",
                "body": "first",
                "user": {"login": "reviewer"},
                "path": "src/foo.py",
                "line": 120,
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 21,
                "node_id": "IC_21",
                "body": "reply",
                "user": {"login": "author"},
                "path": "src/foo.py",
                "line": 120,
                "created_at": "2026-01-01T02:00:00Z",
            },
            {
                "id": 22,
                "node_id": "IC_22",
                "body": "loose",
                "user": {"login": "reviewer"},
                "path": "src/bar.py",
                "line": 3,
                "created_at": "2026-01-01T03:00:00Z",
            },
        ],
        "review-threads.001.json": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "THREAD_1",
                                    "comments": {
                                        "nodes": [{"id": "IC_20"}, {"id": "IC_21"}]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        },
    }
    output = github_output(archive, tmp_path, evidence)
    activity = text(output, "activity.md")
    assert "### `src/foo.py:120`" in activity
    assert activity.index("first") < activity.index("reply")
    assert "loose" in activity
    assert activity.count("first") == 1
    assert activity.count("reply") == 1


def test_active_review_thread_separates_earlier_and_future_comments(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "review-comments.001.json": [
            {
                "id": 19,
                "node_id": "IC_19",
                "body": "before",
                "user": {"login": "reviewer"},
                "created_at": "2025-12-31T15:00:00Z",
            },
            {
                "id": 20,
                "node_id": "IC_20",
                "body": "during",
                "user": {"login": "reviewer"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 21,
                "node_id": "IC_21",
                "body": "after",
                "user": {"login": "author"},
                "created_at": "2026-01-02T01:00:00Z",
            },
        ],
        "review-threads.001.json": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "THREAD_1",
                                    "comments": {
                                        "nodes": [
                                            {"id": "IC_19"},
                                            {"id": "IC_20"},
                                            {"id": "IC_21"},
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        },
    }
    output = github_output(archive, tmp_path, evidence)
    activity = text(output, "activity.md")

    assert "### Earlier context" in activity
    assert "before" in activity
    assert "during" in activity
    assert "after" not in activity
    assert not (output / "github/kfstorm/tracebase/pull/1/background.md").exists()


def test_diff_is_only_in_diff_patch(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            **activity_evidence(),
            "pull-request.diff": "diff --git a/a b/a\n+line\n",
        },
    )
    assert "github/kfstorm/tracebase/pull/1/diff.patch" in files(output)
    diff = text(output, "diff.patch")
    assert "diff --git" in diff
    assert "diff --git" not in text(output, "overview.md")


def test_diff_patch_preserves_raw_bytes(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    raw_diff = b"diff --git a/a b/a\n+trailing  \n\n"
    output = github_output(
        archive,
        tmp_path,
        {"pull-request.diff": raw_diff.decode("utf-8")},
    )

    diff_path = next(output.rglob("diff.patch"))
    assert diff_path.read_bytes() == raw_diff


def test_future_github_events_are_absent_and_background_is_separate(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "comments.001.json": [
            {
                "id": 1,
                "body": "earlier",
                "user": {"login": "a"},
                "created_at": "2025-12-31T15:00:00Z",
            },
            {
                "id": 2,
                "body": "today",
                "user": {"login": "b"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 3,
                "body": "cutoff",
                "user": {"login": "c"},
                "created_at": "2026-01-01T16:00:00Z",
            },
            {
                "id": 4,
                "body": "future",
                "user": {"login": "d"},
                "created_at": "2026-01-02T00:00:00Z",
            },
        ]
    }
    output = github_output(archive, tmp_path, evidence)
    assert "earlier" in text(output, "background.md")
    assert "today" in text(output, "activity.md")
    assert "cutoff" not in text(output, "activity.md")
    assert "future" not in text(output, "activity.md")


def test_github_timestamps_are_local_minute_precision_and_equal_update_once(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "time",
                    "user": {"login": "a"},
                    "created_at": "2025-12-31T23:59:59.123Z",
                    "updated_at": "2025-12-31T23:59:59.123Z",
                }
            ]
        },
    )
    activity = text(output, "activity.md")
    assert "2026-01-01 07:59" in activity
    assert ".123" not in activity
    assert "edited" not in activity


def test_opencode_root_session_uses_title_directory_and_compact_activity(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message("m", "2025-12-31T16:30:00Z", [{"type": "text", "text": "work"}])
            ],
        ),
        "root-run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert any(path.name == "activity.md" for path in output.rglob("activity.md"))
    index = (output / "index.md").read_text()
    assert "/home/kfstorm/dev/tracebase" in index
    assert "Trace work" in index
    activity = text(output, "activity.md")
    assert "Message" not in activity
    assert "m" not in activity
    assert "2026-01-01 00:30" in activity


def test_opencode_child_sessions_are_not_independent_documents(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T01:00:00Z")]),
        "root-run",
    )
    publish_opencode(
        archive,
        session(
            "child", parent_id="root", messages=[message("c", "2026-01-01T01:01:00Z")]
        ),
        "child-run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert "child" not in (output / "index.md").read_text()
    assert len(list(output.rglob("overview.md"))) == 1


def test_task_output_question_and_error_projection(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {
            "type": "tool",
            "tool": "task",
            "description": "inspect",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:00:00Z",
                    "end": "2026-01-01T01:01:00Z",
                },
                "output": (
                    '<task id="ses_child" state="completed">'
                    "<task_result>parent result</task_result></task>"
                ),
                "sessionId": "child",
            },
        },
        {
            "type": "tool",
            "tool": "question",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T02:00:00Z",
                    "end": "2026-01-01T02:01:00Z",
                },
                "input": {"questions": ["Continue?"]},
                "metadata": {"answers": ["yes"]},
            },
        },
        {
            "type": "tool",
            "tool": "read",
            "state": {
                "status": "error",
                "time": {
                    "start": "2026-01-01T03:00:00Z",
                    "end": "2026-01-01T03:01:00Z",
                },
                "input": {"path": "secret"},
                "error": "read failed",
            },
        },
    ]
    activity = opencode_activity(archive, tmp_path, parts, "2026-01-01T00:30:00Z")
    assert "parent result" in activity
    assert "task_result" not in activity
    assert "ses_child" not in activity
    assert "Continue?" in activity and "yes" in activity
    assert "read failed" in activity and "secret" not in activity
    assert "sessionId" not in activity


def test_task_completed_after_cutoff_is_incomplete_without_future_output(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    task = {
        "type": "tool",
        "tool": "task",
        "state": {
            "status": "completed",
            "time": {
                "start": "2026-01-01T15:59:00Z",
                "end": "2026-01-01T16:01:00Z",
            },
            "output": "future result",
        },
    }
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T15:00:00Z", [task])]),
        "run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    activity = text(output, "activity.md")
    assert "Incomplete task." in activity
    assert "future result" not in activity


def test_opencode_cutoff_hides_later_text_answers_and_errors(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {
            "type": "text",
            "text": "future text",
            "time": {"created": "2026-01-01T16:00:00Z"},
        },
        {
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "error",
                "time": {
                    "start": "2026-01-01T15:00:00Z",
                    "end": "2026-01-01T16:01:00Z",
                },
                "output": "future task result",
                "error": "future task error",
            },
        },
        {
            "type": "tool",
            "tool": "question",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T15:30:00Z",
                    "end": "2026-01-01T16:01:00Z",
                },
                "metadata": {"answers": ["future answer"]},
                "input": {"questions": ["Continue?"]},
            },
        },
    ]
    activity = opencode_activity(archive, tmp_path, parts, "2026-01-01T14:00:00Z")

    assert "Incomplete task." in activity
    assert "future text" not in activity
    assert "future task result" not in activity
    assert "future task error" not in activity
    assert "future answer" not in activity


def test_opencode_tool_whitelist_and_path_compaction(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {
            "type": "tool",
            "tool": "apply_patch",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:00:00Z",
                    "end": "2026-01-01T01:01:00Z",
                },
                "input": {
                    "patchText": "*** Update File: src/foo.py\n@@ ignored hunk\n"
                },
            },
        },
        {
            "type": "tool",
            "tool": "edit",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:02:00Z",
                    "end": "2026-01-01T01:03:00Z",
                },
                "input": {
                    "filePath": "/home/kfstorm/dev/tracebase/src/foo.py",
                    "oldString": "old secret",
                    "newString": "new secret",
                },
            },
        },
        {
            "type": "tool",
            "tool": "write",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:04:00Z",
                    "end": "2026-01-01T01:05:00Z",
                },
                "input": {"filePath": "src/bar.py", "content": "body secret"},
            },
        },
        {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:06:00Z",
                    "end": "2026-01-01T01:07:00Z",
                },
                "input": {
                    "command": "pytest",
                    "timeout": 30,
                    "workdir": "/tmp/elsewhere",
                },
            },
        },
        {
            "type": "tool",
            "tool": "grep",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:08:00Z",
                    "end": "2026-01-01T01:09:00Z",
                },
                "input": {"pattern": "hidden"},
            },
        },
    ]
    activity = opencode_activity(archive, tmp_path, parts, "2026-01-01T00:30:00Z")
    assert "src/foo.py" in activity and "src/bar.py" in activity
    assert "@@ ignored hunk" not in activity
    assert "old secret" not in activity and "body secret" not in activity
    assert "pytest" in activity and "timeout" not in activity
    assert "/tmp/elsewhere" in activity
    assert "hidden" not in activity


def test_opencode_equal_workdir_is_omitted_and_relative_paths_are_normalized(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    part = {
        "type": "tool",
        "tool": "bash",
        "state": {
            "status": "completed",
            "time": {"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T01:01:00Z"},
            "input": {"command": "pwd", "workdir": "/home/kfstorm/dev/tracebase"},
        },
    }
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T00:30:00Z", [part])]),
        "run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    activity = text(output, "activity.md")
    assert "Workdir" not in activity


def test_deterministic_output_and_raw_archive_unchanged(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive, session("root", messages=[message("m", "2026-01-01T01:00:00Z")]), "run"
    )
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive.root.rglob("*")
        if path.is_file()
    }
    one, two = tmp_path / "one", tmp_path / "two"
    generate_context(archive.root, request(), one)
    generate_context(archive.root, request(), two)
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive.root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert files(one) == files(two)
    assert b"".join(
        path.read_bytes() for path in sorted(one.rglob("*")) if path.is_file()
    ) == b"".join(
        path.read_bytes() for path in sorted(two.rglob("*")) if path.is_file()
    )


def test_publication_errors_leave_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    original = Path.rename
    monkeypatch.setattr(Path, "rename", lambda *_args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ContextError, match="publication failed"):
        generate_context(archive.root, request(), tmp_path / "output")
    assert not (tmp_path / "output").exists()
    monkeypatch.setattr(Path, "rename", original)


def test_context_generation_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    generate_context(archive, request(), tmp_path / "output")
