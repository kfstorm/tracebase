import json
import shutil
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from tracebase import context_render as context_render_module
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
    extract_context,
    generate_context,
    load_archive,
)
from tracebase.context_render import render_context


def _request() -> ContextRequest:
    return ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")


def _run(
    archive: Archive,
    run_id: str = "run-1",
    from_text: str = "2026-01-01T00:00:00Z",
    to_text: str = "2026-01-02T00:00:00Z",
    source_kind: str = "opencode",
    scope_id: str = "instance-1",
) -> CollectionRun:
    return CollectionRun(
        archive,
        source_kind,
        scope_id,
        CollectionRange.parse(from_text, to_text),
        "test",
        {},
        run_id=run_id,
    )


def _message(
    message_id: str,
    created: str,
    parts: list[dict[str, object]] | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "id": message_id,
        "created": created,
        "role": "user",
        "parts": parts or [],
        **extra,
    }


def _session_payload(
    session_id: str,
    *,
    parent_id: object = None,
    messages: list[dict[str, object]] | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": session_id,
        "messages": messages or [],
        **extra,
    }
    if parent_id is not None:
        payload["parentID"] = parent_id
    return payload


def _publish_opencode(
    run: CollectionRun,
    payload: dict[str, object],
    source_id: str | None = None,
) -> None:
    session_id = source_id or str(payload["id"])
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            session_id,
            run.collection_range.as_manifest(),
            ({"path": "session.json"},),
            metadata={"session": {"id": session_id, "directory": "/archive/project"}},
        )
    )
    run.write_evidence(snapshot, "session.json", json.dumps(payload).encode())
    run.publish({"selected_session_count": 1})


def _publish_github(
    archive: Archive,
    source_id: str,
    evidence: dict[str, object | str],
    *,
    run_id: str = "github-run",
    from_text: str = "2026-01-01T00:00:00Z",
    to_text: str = "2026-01-02T00:00:00Z",
) -> None:
    run = _run(
        archive,
        run_id,
        from_text,
        to_text,
        source_kind="github",
        scope_id="tracked-actor",
    )
    snapshot = run.write_snapshot(
        Snapshot(
            "github",
            "pull-request",
            source_id,
            run.collection_range.as_manifest(),
            tuple({"path": name} for name in evidence),
        )
    )
    for name, value in evidence.items():
        content = value if isinstance(value, str) else json.dumps(value)
        run.write_evidence(snapshot, name, content.encode())
    run.publish({"selected_artifacts": 1, "pagination_complete": True})


def _files(output: Path) -> set[str]:
    return {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }


def _output_bytes(output: Path) -> bytes:
    return b"".join(
        path.read_bytes() for path in sorted(output.rglob("*")) if path.is_file()
    )


def _opencode_view(output: Path, session_id: str = "session-1") -> Path:
    return (
        output
        / "opencode"
        / encode_path_id("instance-1")
        / "session"
        / encode_path_id(session_id)
        / "opencode.md"
    )


def _github_view(output: Path, source_id: str = "PR_1") -> Path:
    return (
        output
        / "github"
        / encode_path_id("tracked-actor")
        / "pull-request"
        / encode_path_id(source_id)
        / "github.md"
    )


def test_context_request_requires_explicit_offsets_and_half_open_order() -> None:
    assert (
        ContextRequest.parse(
            "2026-01-01T00:00:00.000001+00:00", "2026-01-01T00:00:01.000000+00:00"
        ).start
        < ContextRequest.parse(
            "2026-01-01T00:00:00.000001+00:00", "2026-01-01T00:00:01.000000+00:00"
        ).end
    )
    with pytest.raises(ContextError, match="explicit offset"):
        ContextRequest.parse("2026-01-01T00:00:00", "2026-01-01T01:00:00Z")
    with pytest.raises(ContextError, match="before"):
        ContextRequest.parse("2026-01-01T01:00:00Z", "2026-01-01T00:00:00Z")


def test_empty_extraction_publishes_complete_markdown_only_output(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    output = tmp_path / "output"
    generate_context(archive, _request(), output)

    assert _files(output) == {"index.md"}
    index = (output / "index.md").read_text()
    assert 'request_from: "2026-01-01T00:00:00Z"' in index
    assert "No source items are available." in index
    assert "No gaps are available." in index


def test_empty_removed_run_directory_does_not_block_context_generation(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    stale_run = archive / "runs" / "removed-run" / "snapshots"
    stale_run.mkdir(parents=True)

    output = tmp_path / "output"
    generate_context(archive, _request(), output)

    assert _files(output) == {"index.md"}


def test_nonempty_unregistered_run_directory_still_fails(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    invalid_run = archive / "runs" / "invalid-run"
    (invalid_run / "snapshots").mkdir(parents=True)
    (invalid_run / "unregistered.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ContextError, match="unregistered entries"):
        load_archive(archive)


def test_opencode_renderer_is_whitelisted_and_omits_tool_output(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _publish_opencode(
        run,
        _session_payload(
            "session-1",
            projectID="provider-only-marker",
            tokenUsage={"input": 99},
            info={
                "id": "session-1",
                "directory": "/home/example/project",
                "api_key": "credential-marker",
            },
            messages=[
                _message(
                    "message-1",
                    "2026-01-01T00:30:00Z",
                    [
                        {
                            "type": "text",
                            "id": "text-1",
                            "text": (
                                "Useful text. Bearer text-secret "
                                "https://localhost/private https://docs.python.org/3/"
                            ),
                        },
                        {
                            "type": "tool",
                            "id": "tool-1",
                            "tool": "read",
                            "state": {
                                "status": "completed",
                                "time": {
                                    "start": "2026-01-01T00:31:00Z",
                                    "end": "2026-01-01T00:32:00Z",
                                },
                                "input": {
                                    "path": "src/tracebase/context.py",
                                    "api_key": "tool-secret",
                                },
                                "output": "tool-output-marker",
                            },
                        },
                    ],
                )
            ],
        ),
    )
    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    view = _opencode_view(output).read_text()

    assert _files(output) == {
        "index.md",
        f"opencode/{encode_path_id('instance-1')}/session/{encode_path_id('session-1')}/opencode.md",
    }
    assert "Useful text." in view
    assert "https://docs.python.org/3/" in view
    assert "src/tracebase/context.py" in view
    assert "tool-output-marker" not in view
    assert "provider-only-marker" not in view
    assert "tokenUsage" not in view
    assert "credential-marker" not in view
    assert "text-secret" in view
    assert "tool-secret" in view
    assert "https://localhost/private" in view
    assert "## Provenance" in view
    assert "Working directory" in view


def test_unsupported_opencode_parts_are_omitted_without_unknown_part_gap(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _publish_opencode(
        run,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "message-1",
                    "2026-01-01T00:30:00Z",
                    [{"type": "future-part", "id": "future-1", "native": "omitted"}],
                )
            ],
        ),
    )
    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _opencode_view(output).read_text() + (output / "index.md").read_text()

    assert "future-part" not in text
    assert "unknown-part-type" not in text


def test_opencode_gap_whitelist_is_shared_by_item_and_index(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _publish_opencode(
        run,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "message-1",
                    "2026-01-01T00:30:00Z",
                    [
                        {
                            "type": "tool",
                            "id": "task-1",
                            "tool": "task",
                            "state": {
                                "time": {"start": "2026-01-01T00:10:00Z"},
                                "metadata": {
                                    "parentSessionId": "session-1",
                                    "sessionId": "https://private.example/gap?token=gap-secret",
                                },
                            },
                        }
                    ],
                )
            ],
        ),
    )
    output = tmp_path / "output"
    result = extract_context(_request(), load_archive(archive.root))
    item = result.items[0]
    assert item.opencode is not None
    gap = next(
        dict(gap) for gap in item.opencode.gaps if gap["kind"] == "missing-task-child"
    )
    gap["provider_only_marker"] = "must-not-appear"
    modified_item = replace(
        item,
        opencode=replace(item.opencode, gaps=(gap,)),
    )
    render_context(replace(result, items=(modified_item,)), output)
    view = _opencode_view(output).read_text()
    index = (output / "index.md").read_text()

    for text in (view, index):
        assert "https://private.example/gap?token=gap-secret" in text
        assert "gap-secret" in text
        assert "missing-task-child" in text
        assert "session-1" in text
        assert "child_id" in text
        assert "provider_only_marker" not in text


def test_final_observation_state_controls_unknown_completion_gap(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    first = _run(archive, "first", "2025-12-31T00:00:00Z", "2026-01-01T00:00:00Z")
    _publish_opencode(
        first,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "message-1",
                    "2025-12-31T23:00:00Z",
                    [
                        {
                            "type": "tool",
                            "id": "tool-1",
                            "state": {"time": {"start": "2026-01-01T00:30:00Z"}},
                        }
                    ],
                )
            ],
        ),
    )
    second = _run(archive, "second", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z")
    _publish_opencode(
        second,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "message-1",
                    "2025-12-31T23:00:00Z",
                    [
                        {
                            "type": "tool",
                            "id": "tool-1",
                            "state": {
                                "time": {
                                    "start": "2026-01-01T00:30:00Z",
                                    "end": "2026-01-01T00:45:00Z",
                                }
                            },
                        }
                    ],
                )
            ],
        ),
    )
    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _opencode_view(output).read_text()

    assert "unknown-completion" not in text
    assert "End: 2026-01-01T00:45:00+00:00" in text


def test_in_range_task_selects_explicit_child_and_parent_ancestry(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parent = _run(archive, "parent")
    _publish_opencode(
        parent,
        _session_payload(
            "parent-session",
            messages=[
                _message(
                    "parent-message",
                    "2025-12-31T23:59:00Z",
                    [
                        {
                            "type": "tool",
                            "id": "task-1",
                            "tool": "task",
                            "state": {
                                "status": "running",
                                "time": {
                                    "start": "2026-01-01T00:10:00Z",
                                    "end": "2026-01-01T00:20:00Z",
                                },
                                "metadata": {
                                    "parentSessionId": "parent-session",
                                    "sessionId": "child-session",
                                },
                            },
                        }
                    ],
                )
            ],
        ),
    )
    child = _run(archive, "child", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z")
    _publish_opencode(
        child,
        _session_payload(
            "child-session",
            messages=[_message("child-message", "2025-12-31T23:59:00Z")],
        ),
    )

    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    assert _opencode_view(output, "parent-session").is_file()
    assert _opencode_view(output, "child-session").is_file()
    assert "Task relationship" in _opencode_view(output, "parent-session").read_text()


def test_compaction_supporting_context_is_not_presented_as_repeated_work(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _publish_opencode(
        run,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "compaction",
                    "2026-01-01T00:10:00Z",
                    [
                        {
                            "type": "compaction",
                            "id": "compact-1",
                            "tail_start_id": "before",
                        }
                    ],
                ),
                _message(
                    "work",
                    "2026-01-01T00:20:00Z",
                    [{"type": "text", "id": "text-1", "text": "work"}],
                ),
            ],
        ),
    )
    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _opencode_view(output).read_text()
    assert "Supporting context" in text
    assert "not independent repeated work" in text


def test_github_renderer_whitelists_discussion_structure_and_diff_once(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    source_id = "PR_1"
    _publish_github(
        archive,
        source_id,
        {
            "issue.json": {
                "node_id": source_id,
                "number": 1,
                "title": "Harden output",
                "body": (
                    "Keep this body. https://docs.python.org/3/ "
                    "https://private.example/item?token=body-secret"
                ),
                "state": "closed",
                "user": {"login": "author"},
                "created_at": "2025-12-31T23:00:00Z",
                "updated_at": "2026-01-01T00:30:00Z",
                "html_url": "https://private.example/item",
                "provider-only": "not-rendered",
            },
            "pull-request.json": {"node_id": source_id, "merged": False},
            "comments.001.json": [
                {
                    "id": 10,
                    "body": "Comment body",
                    "user": {"login": "commenter"},
                    "created_at": "2026-01-01T00:10:00Z",
                }
            ],
            "reviews.001.json": [
                {
                    "id": 20,
                    "body": "Review body",
                    "state": "APPROVED",
                    "user": {"login": "reviewer"},
                    "submitted_at": "2026-01-01T00:20:00Z",
                }
            ],
            "review-comments.001.json": [
                {
                    "id": 30,
                    "node_id": "inline-1",
                    "pull_request_review_id": 20,
                    "body": "Inline body",
                    "path": "src/context.py",
                    "line": 42,
                    "side": "RIGHT",
                    "user": {"login": "reviewer"},
                    "created_at": "2026-01-01T00:21:00Z",
                }
            ],
            "review-threads.001.json": {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "thread-1",
                                        "isResolved": True,
                                        "isOutdated": False,
                                        "comments": {"nodes": [{"id": "inline-1"}]},
                                    }
                                ]
                            }
                        }
                    }
                }
            },
            "timeline.001.json": [
                {
                    "id": 40,
                    "event": "closed",
                    "actor": {"login": "closer"},
                    "created_at": "2026-01-01T00:25:00Z",
                    "provider_only_marker": "timeline-secret",
                },
                {
                    "id": 50,
                    "event": "cross-referenced",
                    "source": {"issue": {"node_id": "other"}},
                },
            ],
            "pull-request.diff": (
                "diff --git a/context.py b/context.py\n"
                "+ useful change\nsecret=diff-secret\n"
            ),
        },
    )
    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _github_view(output).read_text()

    assert _files(output) == {
        "index.md",
        f"github/{encode_path_id('tracked-actor')}/pull-request/{encode_path_id(source_id)}/github.md",
    }
    for marker in (
        "Harden output",
        "Keep this body.",
        "Comment body",
        "Review body",
        "APPROVED",
        "Inline body",
        "src/context.py",
        "isResolved",
        "Merged: `false`",
        "diff --git",
        "Event: `closed`",
    ):
        assert marker in text
    assert text.count("diff --git") == 1
    assert "cross-referenced" not in text
    assert "timeline-secret" not in text
    assert "provider-only" not in text
    assert "https://private.example/item?token=body-secret" in text
    assert "diff-secret" in text
    assert "review-inline-comment" in text


def test_github_body_history_keeps_distinct_values_and_real_observations(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    source_id = "PR_history"
    base_issue = {
        "node_id": source_id,
        "number": 1,
        "title": "Historical body",
        "state": "open",
        "user": {"login": "author"},
    }
    _publish_github(
        archive,
        source_id,
        {
            "issue.json": {
                **base_issue,
                "body": "old requirement",
                "updated_at": "2026-01-01T00:30:00Z",
            },
            "pull-request.json": {"node_id": source_id, "merged": False},
            "comments.001.json": [],
        },
        run_id="github-first",
    )
    _publish_github(
        archive,
        source_id,
        {
            "issue.json": {
                **base_issue,
                "body": "new requirement",
                "updated_at": "2026-01-02T00:30:00Z",
            },
            "pull-request.json": {"node_id": source_id, "merged": False},
            "comments.001.json": [
                {
                    "id": 99,
                    "body": "appeared in the second snapshot",
                    "user": {"login": "commenter"},
                    "created_at": "2026-01-01T00:40:00Z",
                }
            ],
        },
        run_id="github-second",
        from_text="2026-01-02T00:00:00Z",
        to_text="2026-01-03T00:00:00Z",
    )

    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _github_view(output, source_id).read_text()

    assert "old requirement" in text
    assert "new requirement" in text
    assert "Observation 1" in text
    assert "Observation 2" in text
    assert (
        "- Observation 1 [2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z):\n"
        "  ```text\n  old requirement\n  ```"
    ) in text
    assert (
        "- Observation 2 [2026-01-02T00:00:00Z, 2026-01-03T00:00:00Z):\n"
        "  ```text\n  new requirement\n  ```"
    ) in text
    assert "appeared in the second snapshot" in text
    assert "Observed in: observation 2" in text


def test_pull_request_payload_lifecycle_is_whitelisted(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    source_id = "PR_merged"
    _publish_github(
        archive,
        source_id,
        {
            "issue.json": {
                "node_id": source_id,
                "number": 2,
                "title": "Merged change",
                "state": "closed",
                "user": {"login": "author"},
                "updated_at": "2026-01-01T00:30:00Z",
            },
            "pull-request.json": {
                "node_id": source_id,
                "merged": True,
                "merged_at": "2026-01-01T00:45:00Z",
                "draft": False,
                "mergeable": "MERGEABLE",
                "api_url": "https://private.example/pull/2",
            },
        },
    )

    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _github_view(output, source_id).read_text()

    assert "State: `closed`" in text
    assert "Merged: `true`" in text
    assert "Merged at: 2026-01-01T00:45:00Z" in text
    assert "mergeable" not in text
    assert "private.example" not in text


def test_github_body_current_value_comes_from_latest_observation(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    source_id = "PR_cycle"
    for index, (body, day) in enumerate(
        (("requirement A", "01"), ("requirement B", "02"), ("requirement A", "03")),
        start=1,
    ):
        _publish_github(
            archive,
            source_id,
            {
                "issue.json": {
                    "node_id": source_id,
                    "number": 3,
                    "title": "Restored body",
                    "state": "open",
                    "body": body,
                    "updated_at": f"2026-01-{day}T00:30:00Z",
                },
                "pull-request.json": {"node_id": source_id, "merged": False},
            },
            run_id=f"github-cycle-{index}",
            from_text=f"2026-01-{day}T00:00:00Z",
            to_text=f"2026-01-{int(day) + 1:02d}T00:00:00Z",
        )

    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _github_view(output, source_id).read_text()

    assert "Current observed value:\n\n```text\nrequirement A\n```" in text
    assert (
        "- Observation 1 [2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z), "
        "Observation 3 [2026-01-03T00:00:00Z, 2026-01-04T00:00:00Z):\n"
        "  ```text\n  requirement A\n  ```"
    ) in text
    assert "Observation 2 [2026-01-02T00:00:00Z, 2026-01-03T00:00:00Z)" in text


def test_review_thread_states_retain_observed_versions(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    source_id = "PR_thread_history"
    for index, (resolved, outdated) in enumerate(
        ((False, False), (True, True)), start=1
    ):
        day = f"0{index}"
        _publish_github(
            archive,
            source_id,
            {
                "issue.json": {
                    "node_id": source_id,
                    "number": 4,
                    "title": "Thread state",
                    "state": "open",
                    "updated_at": f"2026-01-{day}T00:30:00Z",
                },
                "pull-request.json": {"node_id": source_id, "merged": False},
                "review-threads.001.json": {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-history",
                                            "isResolved": resolved,
                                            "isOutdated": outdated,
                                            "comments": {"nodes": []},
                                        }
                                    ]
                                }
                            }
                        }
                    }
                },
            },
            run_id=f"github-thread-{index}",
            from_text=f"2026-01-{day}T00:00:00Z",
            to_text=f"2026-01-{int(day) + 1:02d}T00:00:00Z",
        )

    output = tmp_path / "output"
    generate_context(archive.root, _request(), output)
    text = _github_view(output, source_id).read_text()

    assert "- Current isResolved: `true`" in text
    assert "- Current isOutdated: `true`" in text
    assert "- Observed isResolved versions:" in text
    assert "- Observed isOutdated versions:" in text
    assert "Observation 1 [2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z): `false`" in text
    assert "Observation 2 [2026-01-02T00:00:00Z, 2026-01-03T00:00:00Z): `true`" in text


def test_output_is_deterministic_and_excludes_collection_run_ids(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    first = _run(archive, "01a07c6a-ae88-7656-8f31-035370e7fe0d")
    _publish_opencode(
        first,
        _session_payload("session-1", messages=[_message("m", "2026-01-01T00:30:00Z")]),
    )
    one, two = tmp_path / "one", tmp_path / "two"
    generate_context(archive.root, _request(), one)
    generate_context(archive.root, _request(), two)
    assert _files(one) == _files(two)
    assert _output_bytes(one) == _output_bytes(two)
    assert b"01a07c6a-ae88-7656-8f31-035370e7fe0d" not in _output_bytes(one)
    assert not any(path.name.endswith(".json") for path in one.rglob("*"))
    assert not any(path.name == "observations" for path in one.rglob("*"))


def test_changed_and_removed_archive_evidence_is_seen_on_next_invocation(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_opencode(
        _run(archive),
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "m", "2026-01-01T00:30:00Z", [{"type": "text", "text": "first"}]
                )
            ],
        ),
    )
    first = tmp_path / "first"
    generate_context(archive.root, _request(), first)
    shutil.rmtree(next((archive.root / "runs").iterdir()))
    generate_context(archive.root, _request(), tmp_path / "second")
    assert "first" not in (tmp_path / "second" / "index.md").read_text()
    assert _files(tmp_path / "second") == {"index.md"}


def test_missing_declared_evidence_fails_before_publication(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "session-1",
            run.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(snapshot, "session.json", b'{"id":"session-1","messages":[]}')
    run.publish({})
    evidence = (
        next((archive.root / "runs").iterdir())
        / "snapshots"
        / "session"
        / encode_path_id("session-1")
        / "session.json"
    )
    evidence.unlink()
    with pytest.raises(ContextError, match="missing"):
        generate_context(archive.root, _request(), tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_publication_and_cleanup_failures_leave_no_partial_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_opencode(
        _run(archive),
        _session_payload("session-1", messages=[_message("m", "2026-01-01T00:30:00Z")]),
    )

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ContextError, match="already exists"):
        generate_context(archive.root, _request(), existing)

    original_rename = context_render_module.Path.rename
    monkeypatch.setattr(
        context_render_module.Path,
        "rename",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(ContextError, match="publication failed"):
        generate_context(archive.root, _request(), tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert list(tmp_path.glob(".output.*")) == []

    monkeypatch.setattr(context_render_module.Path, "rename", original_rename)
    monkeypatch.setattr(
        context_render_module,
        "write_markdown",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        context_render_module.shutil,
        "rmtree",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(ContextError, match="cleanup failed"):
        generate_context(archive.root, _request(), tmp_path / "cleanup-output")


def test_context_generation_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network access")
        ),
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    generate_context(archive, _request(), tmp_path / "output")
