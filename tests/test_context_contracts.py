import json
import shutil
import urllib.request
from pathlib import Path

import pytest

from tracebase import cli
from tracebase import context as context_module
from tracebase.archive import Archive, CollectionRange, CollectionRun, Snapshot
from tracebase.context import (
    ContextError,
    ContextRequest,
    generate_context,
    load_archive,
)


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


def _snapshot(
    run: CollectionRun,
    source_id: str = "session-1",
    object_kind: str = "session",
    evidence_path: str = "session.json",
) -> None:
    snapshot = run.write_snapshot(
        Snapshot(
            run.source_kind,
            object_kind,
            source_id,
            run.collection_range.as_manifest(),
            ({"path": evidence_path},),
        )
    )
    content = (
        b'{"id":"message-1","messages":[{"id":"message-1",'
        b'"created":"2026-01-01T00:30:00Z","role":"user","parts":[]}]}'
        if run.source_kind == "opencode"
        else b"{}"
    )
    run.write_evidence(snapshot, evidence_path, content)


def _opencode_snapshot(
    run: CollectionRun, payload: dict[str, object], source_id: str | None = None
) -> None:
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            source_id or str(payload["id"]),
            run.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(snapshot, "session.json", json.dumps(payload).encode())


def _context_manifest(
    archive: Archive, request: ContextRequest, output: Path
) -> dict[str, object]:
    generate_context(archive.root, request, output)
    return json.loads((output / "context.json").read_text())


def _output_bytes(output: Path) -> bytes:
    return b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())


def _opencode_item(manifest: dict[str, object], session_id: str) -> dict[str, object]:
    for item in manifest["source_items"]:
        if item["source_id"] == session_id:
            return item
    raise AssertionError(f"missing OpenCode session {session_id}")


def _opencode_projection(
    archive: Archive, request: ContextRequest, output: Path, session_id: str
) -> dict[str, object]:
    manifest = _context_manifest(archive, request, output)
    item = _opencode_item(manifest, session_id)
    return json.loads((output / item["opencode_path"]).read_text())


def _session_payload(
    session_id: str,
    *,
    parent_id: object = None,
    messages: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"id": session_id, "messages": messages or []}
    if parent_id is not None:
        payload["parentID"] = parent_id
    return payload


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


def _publish_tool_observations(
    archive: Archive, observations: list[tuple[str, dict[str, object]]]
) -> None:
    for index, (run_id, state) in enumerate(observations):
        run = _run(
            archive,
            run_id,
            f"2026-01-{index + 1:02d}T00:00:00Z",
            f"2026-01-{index + 2:02d}T00:00:00Z",
        )
        _opencode_snapshot(
            run,
            _session_payload(
                "session-1",
                messages=[
                    _message(
                        "message-1",
                        "2025-12-31T23:00:00Z",
                        [{"type": "tool", "id": "tool-1", "state": state}],
                    )
                ],
            ),
        )
        run.publish({})


def _publish_single_message(archive: Archive, message: dict[str, object]) -> None:
    run = _run(archive)
    _opencode_snapshot(
        run,
        _session_payload("session-1", messages=[message]),
    )
    run.publish({})


def _projected_part(projection: dict[str, object], part_type: str) -> dict[str, object]:
    return next(
        part for part in projection["messages"][0]["parts"] if part["type"] == part_type
    )


def _archive_with_snapshot(root: Path) -> None:
    archive = Archive(root)
    run = _run(archive)
    _snapshot(run)
    run.publish({})


def _published_run_root(root: Path) -> Path:
    _archive_with_snapshot(root)
    return next((root / "runs").iterdir())


def test_context_request_accepts_fractional_offsets_and_half_open_range() -> None:
    request = ContextRequest.parse(
        "2026-01-01T00:00:00.000001+00:00", "2026-01-01T00:00:01.000000+00:00"
    )
    assert request.start < request.end
    with pytest.raises(ContextError):
        ContextRequest.parse("2026-01-01T00:00:00.1234567Z", "2026-01-01T01:00:00Z")
    with pytest.raises(ContextError, match="explicit offset"):
        ContextRequest.parse("2026-01-01T00:00:00", "2026-01-01T01:00:00Z")


def test_empty_archive_is_a_complete_deterministic_output(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    output = tmp_path / "output"
    generate_context(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    assert (output / "index.md").is_file()
    manifest = json.loads((output / "context.json").read_text())
    assert manifest["source_items"] == []
    assert manifest["relations"] == []
    assert manifest["unresolved_references"] == []
    assert manifest["gaps"] == []


def test_context_groups_all_archive_observations_without_payload_interpretation(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    before = _run(archive, "before", "2025-12-30T00:00:00Z", "2025-12-31T00:00:00Z")
    _snapshot(before, source_id="same")
    before.publish({})
    in_range = _run(archive, "in", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    _snapshot(in_range, source_id="same")
    in_range.publish({})
    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T12:00:00Z", "2026-01-01T13:00:00Z"),
        output,
    )
    items = json.loads((output / "context.json").read_text())["source_items"]
    assert items == []


def test_pending_tool_without_start_is_observed_state(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_single_message(
        archive,
        _message(
            "message-1",
            "2026-01-01T00:30:00Z",
            [
                {
                    "type": "tool",
                    "id": "tool-1",
                    "state": {"status": "pending", "input": {}},
                }
            ],
        ),
    )
    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    assert manifest["gaps"] == []
    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "second-output",
        "session-1",
    )
    tool = _projected_part(projection, "tool")
    assert tool["temporal_roles"] == ["observed_state"]
    assert "start" not in tool
    assert "end" not in tool


def test_known_start_without_end_is_explicitly_incomplete(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_single_message(
        archive,
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
        ),
    )

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    tool = _projected_part(projection, "tool")
    assert tool["completion"] == "unknown"
    assert any(gap["kind"] == "unknown-completion" for gap in projection["gaps"])


def test_opencode_required_payload_errors_are_not_rendered_as_gaps(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "malformed",
            run.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(snapshot, "session.json", b'{"messages": {}}')
    run.publish({})
    with pytest.raises(ContextError, match="OpenCode session payload"):
        generate_context(
            archive.root,
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            tmp_path / "output",
        )


def test_opencode_projection_uses_interval_overlap_and_preserves_observations(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    first = _run(archive, "a", "2025-12-31T00:00:00Z", "2026-01-01T00:00:00Z")
    first_payload = {
        "id": "session-1",
        "messages": [
            {
                "id": "message-1",
                "created": "2025-12-31T23:00:00Z",
                "parts": [
                    {
                        "type": "tool",
                        "id": "tool-1",
                        "state": {
                            "time": {
                                "start": "2025-12-31T23:59:00Z",
                                "end": "2026-01-01T00:01:00Z",
                            }
                        },
                    },
                    {
                        "type": "task",
                        "id": "child-in-range",
                        "state": {
                            "status": "running",
                            "time": {"start": "2025-12-31T23:00:00Z"},
                        },
                    },
                ],
            }
        ],
    }
    snapshot = first.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "session-1",
            first.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    first.write_evidence(snapshot, "session.json", json.dumps(first_payload).encode())
    first.publish({})

    second = _run(archive, "b", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    second_payload = {
        "id": "session-1",
        "messages": [
            {
                "id": "message-1",
                "created": "2025-12-31T23:00:00Z",
                "parts": [
                    {
                        "type": "tool",
                        "id": "tool-1",
                        "state": {"time": {"start": "2026-01-01T00:30:00Z"}},
                    },
                    {
                        "type": "task",
                        "id": "child-in-range",
                        "state": {
                            "status": "completed",
                            "time": {"start": "2026-01-01T00:30:00Z"},
                        },
                    },
                ],
            }
        ],
    }
    snapshot = second.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "session-1",
            second.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    second.write_evidence(snapshot, "session.json", json.dumps(second_payload).encode())
    second.publish({})

    output = tmp_path / "output"
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    generate_context(archive.root, request, output)
    manifest = json.loads((output / "context.json").read_text())
    item = manifest["source_items"][0]
    assert item["view_path"].endswith("/opencode.md")
    assert item["opencode_path"].endswith("/opencode.json")
    projection = json.loads((output / item["opencode_path"]).read_text())
    assert "task_children" not in projection
    assert len(projection["messages"]) == 1
    message = projection["messages"][0]
    assert "tools" not in message
    assert len(message["representations"]) == 2
    tool = next(part for part in message["parts"] if part["type"] == "tool")
    assert len(tool["representations"]) == 2
    assert tool["temporal_roles"] == ["in_range_work"]
    assert len(projection["session"]["representations"]) == 2
    assert [
        representation["run_id"]
        for representation in projection["session"]["representations"]
    ] == ["a", "b"]
    task = next(part for part in message["parts"] if part["type"] == "task")
    assert task["id"] == "child-in-range"
    assert len(task["representations"]) == 2
    assert task["representations"][0]["value"]["state"]["status"] == "running"
    assert task["representations"][1]["value"]["state"]["status"] == "completed"
    assert [
        representation["observation_window"]
        for representation in task["representations"]
    ] == [
        {"from": "2025-12-31T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
    ]


@pytest.mark.parametrize(
    "part",
    [
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "time": {
                "start": "2026-01-01T00:04:00Z",
                "end": "2026-01-01T00:08:00Z",
            },
        },
        {
            "type": "text",
            "id": "text-1",
            "time": {
                "start": "2026-01-01T00:08:00Z",
                "end": "2026-01-01T00:09:00Z",
            },
        },
    ],
    ids=["reasoning", "text"],
)
def test_timed_non_tool_part_selects_session(
    tmp_path: Path, part: dict[str, object]
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
        run,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "message-1",
                    "2025-12-31T23:59:00Z",
                    [part],
                )
            ],
        ),
    )
    run.publish({})

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    assert projection["messages"][0]["temporal_roles"] == ["earlier_background"]
    assert projection["messages"][0]["parts"][0]["temporal_roles"] == ["in_range_work"]


def test_parts_are_ordered_by_semantic_time_then_id(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_single_message(
        archive,
        _message(
            "message-1",
            "2026-01-01T00:30:00Z",
            [
                {
                    "type": "text",
                    "id": "text-2",
                    "time": {
                        "start": "2026-01-01T00:20:00Z",
                        "end": "2026-01-01T00:21:00Z",
                    },
                },
                {
                    "type": "reasoning",
                    "id": "reasoning-1",
                    "time": {
                        "start": "2026-01-01T00:10:00Z",
                        "end": "2026-01-01T00:15:00Z",
                    },
                },
            ],
        ),
    )

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    assert [part["id"] for part in projection["messages"][0]["parts"]] == [
        "reasoning-1",
        "text-2",
    ]


def test_native_created_point_selects_session_and_orders_parts(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_single_message(
        archive,
        _message(
            "message-1",
            "2025-12-31T23:59:00Z",
            [
                {
                    "type": "future-part-type",
                    "id": "unknown-1",
                    "someNativeField": "value",
                },
                {
                    "type": "retry",
                    "id": "retry-1",
                    "time": {"created": "2026-01-01T00:10:00Z"},
                },
            ],
        ),
    )

    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    item = _opencode_item(manifest, "session-1")
    projection = json.loads((tmp_path / "output" / item["opencode_path"]).read_text())
    assert item["inclusion_reasons"] == ["in_range_source_record"]
    assert [part["id"] for part in projection["messages"][0]["parts"]] == [
        "retry-1",
        "unknown-1",
    ]
    retry = _projected_part(projection, "retry")
    assert retry["temporal_roles"] == ["in_range_work"]
    assert retry["value"]["time"] == {"created": "2026-01-01T00:10:00Z"}


def test_native_created_point_at_range_end_is_excluded(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_single_message(
        archive,
        _message(
            "message-1",
            "2025-12-31T23:59:00Z",
            [
                {
                    "type": "text",
                    "id": "text-1",
                    "time": {
                        "start": "2026-01-01T00:30:00Z",
                        "end": "2026-01-01T00:31:00Z",
                    },
                },
                {
                    "type": "retry",
                    "id": "retry-1",
                    "time": {"created": "2026-01-01T01:00:00Z"},
                },
            ],
        ),
    )

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    retry = _projected_part(projection, "retry")
    assert retry["temporal_roles"] == ["later_progression"]


def test_unknown_part_is_preserved_as_observed_state(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    unknown = {
        "type": "future-part-type",
        "id": "unknown-1",
        "someNativeField": "value",
    }
    _publish_single_message(
        archive,
        _message("message-1", "2026-01-01T00:10:00Z", [unknown]),
    )

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    part = _projected_part(projection, "future-part-type")
    assert part["type"] == "future-part-type"
    assert part["value"] == unknown
    assert part["temporal_roles"] == ["observed_state"]
    assert any(gap["kind"] == "unknown-part-type" for gap in projection["gaps"])


def test_missing_task_child_is_reported_as_gap(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
        run,
        _session_payload(
            "parent-session",
            messages=[
                _message(
                    "message-1",
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
                                    "sessionId": "child-session",
                                    "parentSessionId": "parent-session",
                                },
                            },
                        }
                    ],
                )
            ],
        ),
    )
    run.publish({})

    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    item = _opencode_item(manifest, "parent-session")
    projection = json.loads((tmp_path / "output" / item["opencode_path"]).read_text())
    assert item["inclusion_reasons"] == ["in_range_source_record"]
    assert {
        (gap["kind"], gap["session_id"], gap["child_id"])
        for gap in projection["gaps"]
        if gap["kind"] == "missing-task-child"
    } == {("missing-task-child", "parent-session", "child-session")}


def test_in_range_task_selects_parent_and_includes_archived_child(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    task = _run(archive, "parent-run")
    _opencode_snapshot(
        task,
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
    task.publish({})
    child = _run(
        archive,
        "child-run",
        "2026-01-02T00:00:00Z",
        "2026-01-03T00:00:00Z",
    )
    _opencode_snapshot(
        child,
        _session_payload(
            "child-session",
            messages=[_message("child-message", "2025-12-31T23:59:00Z")],
        ),
    )
    child.publish({})

    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    assert {item["source_id"] for item in manifest["source_items"]} == {
        "parent-session",
        "child-session",
    }
    assert _opencode_item(manifest, "parent-session")["inclusion_reasons"] == [
        "in_range_source_record"
    ]
    assert _opencode_item(manifest, "child-session")["inclusion_reasons"] == [
        "supporting-task-context"
    ]


def test_selected_session_uses_its_own_task_children(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")

    for index, (session_id, parent_id, child_id) in enumerate(
        [
            ("ancestor", None, "ancestor-child"),
            ("selected", "ancestor", "selected-child"),
        ]
    ):
        run = _run(
            archive,
            f"session-{index}",
            f"2026-01-{index + 1:02d}T00:00:00Z",
            f"2026-01-{index + 2:02d}T00:00:00Z",
        )
        _opencode_snapshot(
            run,
            _session_payload(
                session_id,
                parent_id=parent_id,
                messages=[
                    _message(
                        f"{session_id}-message",
                        "2026-01-01T00:30:00Z"
                        if session_id == "selected"
                        else "2025-12-31T23:00:00Z",
                        [
                            {
                                "type": "tool",
                                "id": f"{session_id}-task",
                                "tool": "task",
                                "state": {
                                    "time": {
                                        "start": (
                                            "2026-01-01T00:30:00Z"
                                            if session_id == "selected"
                                            else "2025-12-31T23:30:00Z"
                                        ),
                                        "end": (
                                            "2026-01-01T00:45:00Z"
                                            if session_id == "selected"
                                            else "2025-12-31T23:45:00Z"
                                        ),
                                    },
                                    "metadata": {
                                        "sessionId": child_id,
                                        "parentSessionId": session_id,
                                    },
                                },
                            }
                        ],
                    )
                ],
            ),
        )
        run.publish({})
        child = _run(
            archive,
            f"{session_id}-child",
            f"2026-01-{index + 3:02d}T00:00:00Z",
            f"2026-01-{index + 4:02d}T00:00:00Z",
        )
        _opencode_snapshot(
            child,
            _session_payload(
                child_id,
                messages=[_message(f"{child_id}-message", "2025-12-31T23:00:00Z")],
            ),
        )
        child.publish({})

    manifest = _context_manifest(archive, request, tmp_path / "output")
    assert {item["source_id"] for item in manifest["source_items"]} == {
        "ancestor",
        "selected",
        "selected-child",
    }
    assert _opencode_item(manifest, "selected-child")["inclusion_reasons"] == [
        "supporting-task-context"
    ]


@pytest.mark.parametrize(
    "message",
    [
        _message(
            "compaction",
            "2026-01-01T00:30:00Z",
            [{"type": "compaction", "tail_start_id": "message-before-compaction"}],
        ),
        _message(
            "continuation",
            "2026-01-01T00:30:00Z",
            [
                {
                    "type": "text",
                    "synthetic": True,
                    "metadata": {"compaction_continue": True},
                    "time": {
                        "start": "2026-01-01T00:30:00Z",
                        "end": "2026-01-01T00:30:01Z",
                    },
                }
            ],
        ),
    ],
    ids=["native-compaction", "synthetic-continuation"],
)
def test_supporting_messages_do_not_select_a_session(
    tmp_path: Path, message: dict[str, object]
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(run, _session_payload("session-1", messages=[message]))
    run.publish({})

    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    assert manifest["source_items"] == []


def test_native_compaction_is_preserved_as_supporting_context(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
        run,
        _session_payload(
            "session-1",
            messages=[
                _message(
                    "compaction",
                    "2026-01-01T00:30:00Z",
                    [
                        {
                            "type": "compaction",
                            "tail_start_id": "message-before-compaction",
                        }
                    ],
                ),
                _message("work", "2026-01-01T00:45:00Z"),
            ],
        ),
    )
    run.publish({})

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    compaction = next(
        message for message in projection["messages"] if message["id"] == "compaction"
    )
    assert compaction["temporal_roles"] == ["observed_state"]
    assert compaction["value"]["parts"] == [
        {"type": "compaction", "tail_start_id": "message-before-compaction"}
    ]


def test_message_payload_order_is_normalized_by_created_time(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
        run,
        _session_payload(
            "session-1",
            messages=[
                _message("later", "2026-01-01T00:40:00Z"),
                _message("earlier", "2026-01-01T00:20:00Z"),
            ],
        ),
    )
    run.publish({})
    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    assert [message["id"] for message in projection["messages"]] == [
        "earlier",
        "later",
    ]


@pytest.mark.parametrize(
    ("started", "selected"),
    [("2026-01-01T00:30:00Z", True), ("2026-01-01T01:00:00Z", False)],
    ids=["in-range", "at-range-end"],
)
def test_zero_duration_tool_uses_half_open_range(
    tmp_path: Path, started: str, selected: bool
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
        run,
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
                            "state": {"time": {"start": started, "end": started}},
                        }
                    ],
                )
            ],
        ),
    )
    run.publish({})
    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    assert bool(manifest["source_items"]) is selected


def test_overlapping_unknown_and_completed_tool_intervals_merge(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_tool_observations(
        archive,
        [
            ("first", {"time": {"start": "2025-12-31T23:30:00Z"}}),
            (
                "second",
                {
                    "time": {
                        "start": "2026-01-01T00:30:00Z",
                        "end": "2026-01-01T00:45:00Z",
                    }
                },
            ),
        ],
    )

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    tool = _projected_part(projection, "tool")
    assert tool["temporal_roles"] == ["in_range_work"]
    assert len(tool["representations"]) == 2


def test_pending_tool_promotes_later_known_start(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _publish_tool_observations(
        archive,
        [
            ("pending", {"status": "pending", "input": {}}),
            (
                "running",
                {"status": "running", "time": {"start": "2026-01-01T00:30:00Z"}},
            ),
        ],
    )

    projection = _opencode_projection(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
        "session-1",
    )
    tool = next(
        part for part in projection["messages"][0]["parts"] if part["type"] == "tool"
    )
    assert len(tool["representations"]) == 2
    assert tool["start"] == "2026-01-01T00:30:00+00:00"
    assert tool["temporal_roles"] == ["in_range_work"]


@pytest.mark.parametrize("case", ["missing", "malformed", "cyclic"])
def test_session_ancestry_gaps_are_explicit(tmp_path: Path, case: str) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    if case == "missing":
        sessions = [("selected", "does-not-exist")]
    elif case == "malformed":
        sessions = [("selected", 42)]
    else:
        sessions = [("selected", "parent"), ("parent", "selected")]
    for index, (session_id, parent_id) in enumerate(sessions):
        run = _run(
            archive,
            f"run-{index}",
            f"2026-01-{index + 1:02d}T00:00:00Z",
            f"2026-01-{index + 2:02d}T00:00:00Z",
        )
        _opencode_snapshot(
            run,
            _session_payload(
                session_id,
                parent_id=parent_id,
                messages=[_message(f"{session_id}-message", "2026-01-01T00:30:00Z")],
            ),
        )
        run.publish({})

    manifest = _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )
    selected = _opencode_item(manifest, "selected")
    projection = json.loads(
        (tmp_path / "output" / selected["opencode_path"]).read_text()
    )
    gap_kinds = {gap["kind"] for gap in projection["gaps"]}
    expected = {
        "missing": "missing-session-parent",
        "malformed": "malformed-session-parent",
        "cyclic": "cyclic-session-parent",
    }[case]
    assert expected in gap_kinds
    assert expected in {gap["kind"] for gap in manifest["gaps"]}
    assert expected in (tmp_path / "output" / "index.md").read_text()


def test_uuid_collection_ids_do_not_become_output_identity_or_paths(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run_id = "01a07c6a-ae88-7656-8f31-035370e7fe0d"
    run = _run(archive, run_id)
    _snapshot(run)
    run.publish({})

    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    assert run_id.encode() not in _output_bytes(output)
    assert any(
        "observations/observation-001" in path.as_posix() for path in output.rglob("*")
    )


def test_context_output_redacts_credentials_and_private_urls(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
        run,
        {
            "id": "session-1",
            "info": {
                "id": "session-1",
                "directory": "/home/example/project",
                "api_key": "credential-sentinel",
            },
            "messages": [
                _message(
                    "message-1",
                    "2026-01-01T00:30:00Z",
                    [
                        {
                            "type": "text",
                            "id": "text-1",
                            "text": (
                                "Bearer secret-sentinel https://localhost:8443/private "
                                "https://private.example/internal"
                            ),
                        }
                    ],
                )
            ],
        },
    )
    run.publish({})

    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    output_bytes = _output_bytes(output)
    assert b"credential-sentinel" not in output_bytes
    assert b"secret-sentinel" not in output_bytes
    assert b"https://localhost:8443/private" not in output_bytes
    assert b"https://private.example/internal" not in output_bytes
    assert b"/home/example/project" in output_bytes


def test_root_gaps_use_the_same_sanitized_output_boundary(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _opencode_snapshot(
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
    run.publish({})

    output = tmp_path / "output"
    _context_manifest(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    output_bytes = _output_bytes(output)
    assert b"https://private.example/gap?token=gap-secret" not in output_bytes
    assert b"gap-secret" not in output_bytes


def test_github_context_projects_native_records_without_fix_inference(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(
        archive,
        "github-run",
        source_kind="github",
        scope_id="tracked-actor",
    )
    source_id = "PR_1"
    evidence = {
        "issue.json": {
            "node_id": source_id,
            "created_at": "2025-12-31T23:00:00Z",
            "updated_at": "2026-01-01T00:30:00Z",
            "body": (
                "See https://github.com/example/repo/issues/99 and "
                "https://github.com/example/repo/issues/99, not "
                "https://github.com/example/repo/issues/99x, "
                "xhttps://github.com/example/repo/issues/99, or "
                "https://github.com/example-/repo/issues/99; then "
                "<https://github.com/example/repo/issues/99>"
            ),
            "html_url": "https://github.com/example/repo/pull/1",
            "user": {"login": "author"},
        },
        "pull-request.json": {"node_id": source_id},
        "comments.001.json": [
            {
                "id": 10,
                "created_at": "2025-12-31T23:15:00Z",
                "updated_at": "2026-01-01T00:25:00Z",
                "user": {"login": "reviewer"},
            }
        ],
        "timeline.001.json": [
            {"id": 10, "event": "commented"},
            {"id": 20, "event": "reviewed", "actor": {"login": "reviewer"}},
            {"id": 40, "event": "closed", "actor": {"login": "closer"}},
            {"id": 60, "event": "labeled", "created_at": "2025-12-31T23:50:00Z"},
            {
                "id": 50,
                "event": "cross-referenced",
                "source": {"issue": {"node_id": "SOURCE_PR"}},
            },
            {
                "node_id": "commit-first",
                "event": "committed",
                "author": {"date": "2025-12-31T23:00:00Z"},
                "committer": {"date": "2026-01-01T01:00:00+01:00"},
            },
            {
                "node_id": "commit-second",
                "event": "committed",
                "author": {"date": "2025-12-31T23:00:00Z"},
                "committer": {"date": "2026-01-01T00:30:00Z"},
            },
        ],
        "reviews.001.json": [
            {
                "id": 20,
                "submitted_at": "2026-01-01T00:20:00Z",
                "user": {"login": "reviewer"},
            }
        ],
        "review-comments.001.json": [
            {
                "id": 30,
                "node_id": "comment-node",
                "pull_request_review_id": 20,
                "created_at": "2026-01-01T00:21:00Z",
                "user": {"login": "reviewer"},
            },
            {
                "id": 31,
                "node_id": "reply-node",
                "in_reply_to_id": 30,
                "created_at": "2026-01-01T00:22:00Z",
                "user": {"login": "reviewer"},
            },
        ],
        "review-threads.001.json": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread-node",
                                    "isResolved": True,
                                    "isOutdated": True,
                                    "comments": {"nodes": [{"id": "comment-node"}]},
                                }
                            ]
                        }
                    }
                }
            }
        },
        "review-thread-comments.001.002.json": {
            "data": {
                "node": {
                    "id": "thread-node",
                    "comments": {"nodes": [{"id": "reply-node"}]},
                }
            }
        },
        "pull-request.diff": ('diff --git a/a b/a\npassword: "diff-secret-sentinel"\n'),
    }
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
    run.publish({})
    later = _run(
        archive,
        "github-run-later",
        "2026-01-02T00:00:00Z",
        "2026-01-03T00:00:00Z",
        source_kind="github",
        scope_id="tracked-actor",
    )
    later_snapshot = later.write_snapshot(
        Snapshot(
            "github",
            "pull-request",
            source_id,
            later.collection_range.as_manifest(),
            ({"path": "issue.json"}, {"path": "pull-request.json"}),
        )
    )
    later.write_evidence(
        later_snapshot,
        "issue.json",
        json.dumps(
            {
                "node_id": source_id,
                "created_at": "2025-12-31T23:00:00Z",
                "updated_at": "2026-01-02T00:45:00Z",
            }
        ).encode(),
    )
    later.write_evidence(later_snapshot, "pull-request.json", b'{"node_id": "PR_1"}')
    later.publish({})

    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    manifest = json.loads((output / "context.json").read_text())
    item = manifest["source_items"][0]
    projection = json.loads((output / item["github_path"]).read_text())
    assert item["inclusion_reasons"] == ["in_range_source_record"]
    thread = next(
        record for record in projection["records"] if record["kind"] == "review-thread"
    )
    assert thread["representations"][0]["value"]["isResolved"]
    assert {relation["kind"] for relation in projection["relations"]} == {
        "inline-reply",
        "review-inline-comment",
        "thread-inline-comment",
    }
    assert next(
        record for record in projection["records"] if record["kind"] == "aggregate-diff"
    )["limitations"] == ["does_not_establish_fix_or_commit"]
    assert next(
        record
        for record in projection["records"]
        if record["kind"] == "ordinary-comment"
    )["temporal_roles"] == ["in_range_work", "earlier_background"]
    ordinary_comment = next(
        record
        for record in projection["records"]
        if record["kind"] == "ordinary-comment"
    )
    assert len(ordinary_comment["representations"]) == 2
    review_index = next(
        index
        for index, record in enumerate(projection["records"])
        if record["kind"] == "review"
    )
    comment_index = next(
        index
        for index, record in enumerate(projection["records"])
        if record["kind"] == "ordinary-comment"
    )
    assert review_index < comment_index
    earlier_index = next(
        index
        for index, record in enumerate(projection["records"])
        if record["native_id"] == "60"
    )
    assert earlier_index < review_index
    review = next(
        record for record in projection["records"] if record["kind"] == "review"
    )
    assert len(review["representations"]) == 2
    assert review["actor"] == {"login": "reviewer"}
    lifecycle = next(
        record for record in projection["records"] if record["native_id"] == "40"
    )
    assert lifecycle["actor"] == {"login": "closer"}
    first_commit = next(
        record
        for record in projection["records"]
        if record["native_id"] == "commit-first"
    )
    assert first_commit["temporal_roles"] == ["in_range_work"]
    record_ids = [record["native_id"] for record in projection["records"]]
    assert record_ids.index("commit-first") < record_ids.index("commit-second")
    pull_request = next(
        record for record in projection["records"] if record["kind"] == "pull-request"
    )
    assert "actor" not in pull_request
    assert {
        representation["run_id"] for representation in pull_request["representations"]
    } == {
        "github-run",
        "github-run-later",
    }
    assert pull_request["temporal_roles"] == [
        "in_range_work",
        "earlier_background",
        "later_progression",
    ]
    assert (
        "Aggregate diffs do not establish a fix"
        in (output / item["view_path"]).read_text()
    )
    assert manifest["relations"] == []
    assert manifest["unresolved_references"] == []
    assert b"diff-secret-sentinel" not in _output_bytes(output)
    issue = next(
        record for record in projection["records"] if record["kind"] == "pull-request"
    )
    assert (
        "https://github.com/example/repo/issues/99"
        in issue["representations"][0]["value"]["body"]
    )
    cross_reference = next(
        record for record in projection["records"] if record["native_id"] == "50"
    )
    assert cross_reference["representations"][0]["value"] == {
        "id": 50,
        "event": "cross-referenced",
        "source": {"issue": {"node_id": "SOURCE_PR"}},
    }
    assert "fixed" not in (output / item["view_path"]).read_text().lower()


def test_scope_aware_identity_and_source_paths_are_deterministic(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    first = _run(archive, "one", scope_id="first")
    _snapshot(first, source_id="same")
    first.publish({})
    second = _run(archive, "two", scope_id="second")
    _snapshot(second, source_id="same")
    second.publish({})
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    one, two = tmp_path / "one", tmp_path / "two"
    generate_context(archive.root, request, one)
    generate_context(archive.root, request, two)
    assert (one / "context.json").read_bytes() == (two / "context.json").read_bytes()
    items = json.loads((one / "context.json").read_text())["source_items"]
    assert {item["source_scope_id"] for item in items} == {"first", "second"}
    assert all((one / item["view_path"]).is_file() for item in items)
    assert all(
        "run_manifest" not in entry for item in items for entry in item["provenance"]
    )
    assert all("coverage" in entry for item in items for entry in item["provenance"])


def test_shared_archive_loader_accepts_unknown_provider_payloads(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive, source_kind="future-provider", scope_id="scope")
    _snapshot(
        run,
        source_id="opaque",
        object_kind="future-object",
        evidence_path="payload.bin",
    )
    run.publish({})
    loaded = load_archive(archive.root)
    assert loaded[0].snapshots[0].evidence == {"payload.bin": b"{}"}
    with pytest.raises(ContextError, match="unsupported context source"):
        generate_context(
            archive.root,
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            tmp_path / "output",
        )


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_context_rejects_an_invalid_archive_root(tmp_path: Path, kind: str) -> None:
    archive = tmp_path / "archive"
    if kind == "file":
        archive.write_text("not an archive")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        archive.symlink_to(target, target_is_directory=True)
    with pytest.raises(ContextError, match="archive root"):
        generate_context(
            archive,
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            tmp_path / "output",
        )


def test_archive_loader_accepts_fractional_observation_windows(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    _snapshot(run)
    published = run.publish({})
    manifest_path = (
        next((published / "snapshots" / "session").iterdir()) / "snapshot.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["observation_window"] = {
        "from": "2026-01-01T00:00:00.000001Z",
        "to": "2026-01-02T00:00:00.000001Z",
    }
    manifest_path.write_text(json.dumps(manifest))
    assert load_archive(archive.root)


def test_archive_rejects_invalid_published_structure(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    run_root = _published_run_root(archive_root)
    (run_root / "unexpected.json").write_text("{}")
    with pytest.raises(ContextError, match="unregistered"):
        load_archive(archive_root)


def test_archive_rejects_path_like_types_and_unregistered_snapshot_tree(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    run_root = _published_run_root(archive_root)
    manifest_path = run_root / "run.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"]["kind"] = "../outside"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ContextError, match="source kind"):
        load_archive(archive_root)

    manifest["source"]["kind"] = "opencode"
    manifest_path.write_text(json.dumps(manifest))
    (run_root / "snapshots" / "session" / "unregistered").mkdir()
    with pytest.raises(ContextError, match="snapshot directories"):
        load_archive(archive_root)


@pytest.mark.parametrize("level", ["runs", "run", "snapshot", "evidence"])
def test_archive_rejects_symlinks_at_every_published_level(
    tmp_path: Path, level: str
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    run_root = _published_run_root(archive_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    if level == "runs":
        shutil.rmtree(archive_root / "runs")
        (archive_root / "runs").symlink_to(outside, target_is_directory=True)
    elif level == "run":
        shutil.rmtree(run_root)
        run_root.symlink_to(outside, target_is_directory=True)
    elif level == "snapshot":
        snapshot_root = next((run_root / "snapshots" / "session").iterdir())
        shutil.rmtree(snapshot_root)
        snapshot_root.symlink_to(outside, target_is_directory=True)
    else:
        evidence = next((run_root / "snapshots" / "session").iterdir()) / "session.json"
        evidence.unlink()
        evidence.symlink_to(outside / "source.json")
    with pytest.raises(ContextError, match=r"symlink|regular|snapshots"):
        load_archive(archive_root)


def test_archive_rejects_a_dangling_runs_symlink(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "runs").symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ContextError, match="runs root"):
        load_archive(archive_root)


def test_snapshot_path_and_observation_window_are_validated(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    run_root = _published_run_root(archive_root)
    manifest_path = run_root / "run.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["snapshots"][0]["path"] = "snapshots/session/not-the-source-id"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ContextError, match="snapshots"):
        load_archive(archive_root)

    other = tmp_path / "second-archive"
    other.mkdir()
    _archive_with_snapshot(other)
    second_root = next((other / "runs").iterdir())
    snapshot_path = (
        next((second_root / "snapshots" / "session").iterdir()) / "snapshot.json"
    )
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["observation_window"]["from"] = "2026-01-01T00:00:00"
    snapshot_path.write_text(json.dumps(snapshot))
    with pytest.raises(ContextError, match="observation window"):
        load_archive(other)


def test_context_refresh_does_not_retain_removed_archive_items(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_snapshot(archive)
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    generate_context(archive, request, tmp_path / "first")
    shutil.rmtree(next((archive / "runs").iterdir()))
    generate_context(archive, request, tmp_path / "second")
    assert (
        json.loads((tmp_path / "second" / "context.json").read_text())["source_items"]
        == []
    )


def test_context_generation_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_urlopen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_snapshot(archive)
    generate_context(
        archive,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        tmp_path / "output",
    )


def test_context_publication_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_snapshot(archive)

    def fail_rename(*_args: object) -> None:
        raise OSError()

    monkeypatch.setattr(context_module, "_rename_without_replacement", fail_rename)
    with pytest.raises(ContextError, match="publication failed"):
        generate_context(
            archive,
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            tmp_path / "output",
        )
    assert not (tmp_path / "output").exists()
    assert list(tmp_path.glob(".output.*")) == []


def test_context_cleanup_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_snapshot(archive)

    def fail_write(*_args: object) -> None:
        raise OSError()

    def fail_cleanup(*_args: object) -> None:
        raise OSError()

    monkeypatch.setattr(context_module, "_write_json", fail_write)
    monkeypatch.setattr(context_module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(ContextError, match="cleanup failed"):
        generate_context(
            archive,
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            tmp_path / "output",
        )


def test_cli_structural_diagnostics_do_not_expose_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_path = tmp_path / "private-archive"

    def fail(*_args: object, **_kwargs: object) -> Path:
        raise OSError(str(secret_path))

    monkeypatch.setattr(cli, "generate_context", fail)
    assert (
        cli.main(
            [
                "context",
                "--archive",
                str(secret_path),
                "--from",
                "2026-01-01T00:00:00Z",
                "--to",
                "2026-01-01T01:00:00Z",
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == "context operation failed\n"
