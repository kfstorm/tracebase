"""Protocol failure tests; real-CLI acceptance lives in integration/opencode."""

import io
import json
from dataclasses import replace

import pytest

from tracebase.archive import (
    Archive,
    ArchiveError,
    CollectionRange,
    CollectionRun,
    load_published_archive,
)
from tracebase.context import ContextRequest, extract_context
from tracebase.opencode import collect
from tracebase.progress import LineProgressSink, ProgressReporter

SESSION = {
    "id": "ses_example",
    "projectID": "project_example",
    "time": {"created": 1767225600000, "updated": 1767225600000},
    "location": {"directory": "/workspace/example"},
}

PROJECT = {
    "id": "project_example",
    "canonical": "/workspace",
    "extra": {"native": True},
}
MESSAGE = {
    "id": "msg_example",
    "type": "user",
    "text": "Synthetic question",
    "time": {"created": 1767225600000},
}


def make_run(tmp_path):
    return CollectionRun(
        Archive(tmp_path / "archive"),
        "opencode",
        "synthetic-instance",
        CollectionRange.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        "test",
        {},
    )


def mock_api_responses(monkeypatch, responses):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.99.0")
    responses = iter(responses)
    monkeypatch.setattr(
        "tracebase.opencode_v2.run_opencode",
        lambda args: json.dumps(
            [PROJECT] if args[2] == "project.list" else next(responses)
        ).encode(),
    )


def assert_unpublished(run):
    with pytest.raises(ArchiveError):
        result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
        run.publish(result.coverage)
    assert not list((run.archive.root / "runs").glob("*"))


def test_empty_v2_collection_skips_project_lookup_and_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.3.0")

    def execute(args):
        assert args[2] == "session.list"
        return b'{"data": [], "cursor": {}}'

    monkeypatch.setattr("tracebase.opencode_v2.run_opencode", execute)
    run = make_run(tmp_path)
    result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
    run.publish(result.coverage)
    assert result.coverage["project_lookup"]["request_count"] == 0
    assert load_published_archive(run.archive.root)[0].snapshots == ()


def test_project_list_is_fetched_once_and_related_native_records_are_read_offline(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.3.0")
    second = SESSION | {"id": "ses_second", "location": {"directory": "/feature"}}
    native = (
        b'{ "extra": {"native": true}, "id": "project_example", '
        b'"canonical": "/workspace" }'
    )
    calls = []

    def execute(args):
        calls.append(args)
        operation = args[2]
        if operation == "project.list":
            assert args[3:] == ["--param", "archived=true"]
            return b" \n [" + native + b', {"id":"unrelated","canonical":"/elsewhere"}]'
        if operation == "session.list":
            return json.dumps({"data": [SESSION, second], "cursor": {}}).encode()
        if operation == "session.get":
            return json.dumps(
                {"data": second if "sessionID=ses_second" in args else SESSION}
            ).encode()
        assert operation == "session.message.list"
        return json.dumps({"data": [MESSAGE], "cursor": {}}).encode()

    monkeypatch.setattr("tracebase.opencode_v2.run_opencode", execute)
    run = make_run(tmp_path)
    result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
    run.publish(result.coverage)
    runs = load_published_archive(run.archive.root)
    assert [args[2] for args in calls].count("project.list") == 1
    for snapshot in runs[0].snapshots:
        assert snapshot.evidence["project.json"] == native
        assert snapshot.manifest["metadata"]["project_lookup"] == {
            "operation": "project.list",
            "parameters": {"archived": "true"},
            "selection": {"id": "project_example"},
        }
    before = len(calls)
    context = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"), runs
    )
    assert len(calls) == before
    assert {item.projection.session["project_directory"] for item in context.items} == {
        "/workspace"
    }
    assert {item.projection.session["working_directory"] for item in context.items} == {
        "/workspace/example",
        "/feature",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"hidden": True},
        {"hidden": False},
        {"hidden": "true"},
        {"source": "synthetic"},
    ],
)
def test_v2_open_metadata_is_not_a_hidden_protocol_and_auxiliary_types_stay_out(
    tmp_path, monkeypatch, metadata
):
    messages = []
    for timestamp in (1767225599000, 1767225601000):
        for role in ("user", "assistant"):
            message = MESSAGE | {
                "id": f"msg_{timestamp}_{role}",
                "type": role,
                "time": {"created": timestamp},
            }
            if role == "assistant":
                message.pop("text")
                message["content"] = [
                    {"type": "text", "text": "Synthetic answer"},
                    {"type": "reasoning", "text": "Excluded reasoning"},
                    {"type": "tool", "text": "Excluded tool output"},
                ]
            if metadata is not None:
                message["metadata"] = metadata
            messages.append(message)
    messages.extend(
        MESSAGE | {"id": f"msg_{kind}", "type": kind, "metadata": {"hidden": False}}
        for kind in ("synthetic", "compaction", "shell", "system", "skill", "idle")
    )
    messages.append(
        MESSAGE
        | {
            "id": "msg_tool_only",
            "type": "assistant",
            "content": [{"type": "tool", "text": "Excluded tool"}],
        }
    )
    mock_api_responses(
        monkeypatch,
        [
            {"data": [SESSION], "cursor": {}},
            {"data": SESSION},
            {"data": messages, "cursor": {}},
        ],
    )
    run = make_run(tmp_path)
    result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
    run.publish(result.coverage)
    runs = load_published_archive(run.archive.root)
    raw_messages = json.loads(runs[0].snapshots[0].evidence["messages/000000.json"])[
        "data"
    ]
    assert raw_messages == messages
    context = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"), runs
    )
    assert len(context.items) == 1
    dialogue = context.items[0].projection.dialogue
    for bucket in (dialogue.activity, dialogue.background):
        assert [(turn.role, turn.text) for turn in bucket] == [
            ("user", "Synthetic question"),
            ("assistant", "Synthetic answer"),
        ]


@pytest.mark.parametrize(
    "project, project_id, gap",
    [
        (None, "project_example", "missing-project"),
        (None, None, "missing-project-id"),
        ({"id": "global", "canonical": "/"}, "global", None),
        (
            PROJECT | {"canonical": "relative/path"},
            "project_example",
            "invalid-project-directory",
        ),
        (PROJECT | {"canonical": "/"}, "project_example", "invalid-project-directory"),
        (PROJECT | {"canonical": None}, "project_example", "invalid-project-directory"),
        (
            PROJECT | {"canonical": "/repo/../wrong"},
            "project_example",
            "invalid-project-directory",
        ),
    ],
)
def test_project_fallback_is_offline_and_records_acquisition_gaps(
    tmp_path, monkeypatch, project, project_id, gap
):
    session = SESSION | {"projectID": project_id}
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.3.0")
    responses = {
        "session.list": {"data": [session], "cursor": {}},
        "project.list": [project] if project else [],
        "session.get": {"data": session},
        "session.message.list": {"data": [MESSAGE], "cursor": {}},
    }
    monkeypatch.setattr(
        "tracebase.opencode_v2.run_opencode",
        lambda args: json.dumps(responses[args[2]]).encode(),
    )
    run = make_run(tmp_path)
    result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
    run.publish(result.coverage)
    runs = load_published_archive(run.archive.root)
    snapshot = runs[0].snapshots[0]
    gaps = snapshot.manifest["metadata"].get("acquisition_gaps", [])
    assert [item["kind"] for item in gaps] == ([gap] if gap else [])
    assert result.coverage["project_lookup"]["gaps"] == gaps
    assert ("project.json" in snapshot.evidence) == (project is not None)
    context = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"), runs
    )
    assert (
        context.items[0].projection.session["project_directory"] == "/workspace/example"
    )


@pytest.mark.parametrize(
    "response",
    [None, {"data": {}}, [PROJECT, PROJECT], [{"id": []}]],
)
def test_failed_or_invalid_project_lookup_never_publishes(
    tmp_path, monkeypatch, response
):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.3.0")

    def execute(args):
        if args[2] == "session.list":
            return json.dumps({"data": [SESSION], "cursor": {}}).encode()
        assert args[2] == "project.list"
        if response is None:
            raise ArchiveError("OpenCode CLI operation failed")
        return json.dumps(response).encode()

    monkeypatch.setattr("tracebase.opencode_v2.run_opencode", execute)
    assert_unpublished(make_run(tmp_path))


@pytest.mark.parametrize("version", [b"3.0.0", b"0.99.0", b"invalid", b"2.3.0-beta"])
def test_unknown_major_or_nonrelease_fails_before_discovery(
    tmp_path, monkeypatch, version
):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: version)
    with pytest.raises(ArchiveError, match="Unsupported OpenCode major"):
        collect(make_run(tmp_path), ProgressReporter(LineProgressSink(io.StringIO())))


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": [], "cursor": []},
        {"data": [], "cursor": {"next": "opaque"}},
        {"data": [SESSION], "cursor": {"next": 123}},
        {"data": [SESSION, SESSION], "cursor": {}},
        {"data": [SESSION | {"time": {"created": True, "updated": 1}}], "cursor": {}},
        {"data": [SESSION | {"location": None}], "cursor": {}},
    ],
)
def test_malformed_discovery_never_publishes(tmp_path, monkeypatch, response):
    mock_api_responses(monkeypatch, [response])
    assert_unpublished(make_run(tmp_path))


def test_opaque_cursor_is_forwarded_without_order_and_cycles_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.99.0")
    calls = []
    opaque = "opaque:+/?=cursor"

    def execute(args):
        calls.append(args)
        return json.dumps({"data": [SESSION], "cursor": {"next": opaque}}).encode()

    monkeypatch.setattr("tracebase.opencode_v2.run_opencode", execute)
    with pytest.raises(ArchiveError, match="incomplete"):
        collect(make_run(tmp_path), ProgressReporter(LineProgressSink(io.StringIO())))
    assert "cursor=" + opaque in calls[1]
    assert "order=asc" not in calls[1]


@pytest.mark.parametrize(
    "message",
    [
        {"type": "unknown"},
        {"type": "user", "text": None},
        {"type": "assistant", "content": [{"type": "text", "text": None}]},
        {"type": "assistant", "content": [{"type": "unknown"}]},
    ],
)
def test_unsupported_message_aborts_collection(tmp_path, monkeypatch, message):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.99.0")
    message = {"id": "msg_example", "time": {"created": 1767225600000}} | message
    responses = iter(
        [
            {"data": [SESSION], "cursor": {}},
            {"data": SESSION},
            {"data": [message], "cursor": {}},
        ]
    )
    monkeypatch.setattr(
        "tracebase.opencode_v2.run_opencode",
        lambda args: json.dumps(
            [PROJECT] if args[2] == "project.list" else next(responses)
        ).encode(),
    )
    run = make_run(tmp_path)
    with pytest.raises(ArchiveError):
        collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
    assert run.snapshot_count == 0


@pytest.mark.parametrize("metadata", [None, {}, SESSION | {"id": "ses_other"}])
def test_session_get_must_return_the_selected_session(tmp_path, monkeypatch, metadata):
    mock_api_responses(
        monkeypatch, [{"data": [SESSION], "cursor": {}}, {"data": metadata}]
    )
    assert_unpublished(make_run(tmp_path))


def test_message_page_failure_after_another_snapshot_never_publishes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tracebase.opencode._run_opencode", lambda args: b"2.3.0")
    second = SESSION | {"id": "ses_second"}
    message = {
        "id": "msg_example",
        "type": "user",
        "text": "Synthetic question",
        "time": {"created": 1767225600000},
    }
    responses = iter(
        [
            {"data": [SESSION, second], "cursor": {}},
            {"data": SESSION},
            {"data": [], "cursor": {}},
            {"data": second},
            {"data": [message], "cursor": {"next": "opaque:+/?=cursor"}},
        ]
    )

    def execute(args):
        if args[2] == "project.list":
            return json.dumps([PROJECT]).encode()
        if any(arg.startswith("cursor=") for arg in args):
            raise ArchiveError("OpenCode command failed")
        return json.dumps(next(responses)).encode()

    monkeypatch.setattr("tracebase.opencode_v2.run_opencode", execute)
    run = make_run(tmp_path)
    with pytest.raises(ArchiveError, match="command failed"):
        result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
        run.publish(result.coverage)
    assert run.snapshot_count == 1
    assert not list((run.archive.root / "runs").glob("*"))


@pytest.fixture
def archived_v2(tmp_path, monkeypatch):
    mock_api_responses(
        monkeypatch,
        [
            {"data": [SESSION], "cursor": {}},
            {"data": SESSION},
            {"data": [MESSAGE], "cursor": {}},
        ],
    )
    run = make_run(tmp_path)
    result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
    run.publish(result.coverage)
    return load_published_archive(run.archive.root)[0]


@pytest.mark.parametrize(
    "mutation", ["identity", "global-identity", "provenance", "malformed"]
)
def test_offline_project_evidence_rejects_mismatched_identity_or_provenance(
    archived_v2, mutation
):
    snapshot = archived_v2.snapshots[0]
    evidence = dict(snapshot.evidence)
    metadata = dict(snapshot.manifest["metadata"])
    if mutation in {"identity", "global-identity"}:
        evidence["project.json"] = json.dumps(
            PROJECT
            | {"id": "global" if mutation == "global-identity" else "another-project"}
        ).encode()
    elif mutation == "provenance":
        metadata.pop("project_lookup")
    else:
        evidence["project.json"] = b"invalid JSON"
    changed = replace(
        snapshot, evidence=evidence, manifest=snapshot.manifest | {"metadata": metadata}
    )
    with pytest.raises(ArchiveError, match="Project evidence"):
        extract_context(
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            (replace(archived_v2, snapshots=(changed,)),),
        )


def test_older_v2_snapshot_without_project_evidence_keeps_directory_fallback(
    archived_v2,
):
    snapshot = archived_v2.snapshots[0]
    evidence = dict(snapshot.evidence)
    evidence.pop("project.json")
    metadata = dict(snapshot.manifest["metadata"])
    metadata.pop("project_lookup")
    older = replace(
        snapshot, evidence=evidence, manifest=snapshot.manifest | {"metadata": metadata}
    )
    context = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        (replace(archived_v2, snapshots=(older,)),),
    )
    assert (
        context.items[0].projection.session["project_directory"] == "/workspace/example"
    )
