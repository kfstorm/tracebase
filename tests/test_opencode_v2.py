"""Protocol failure tests; real-CLI acceptance lives in integration/opencode."""

import io
import json

import pytest

from tracebase.archive import Archive, ArchiveError, CollectionRange, CollectionRun
from tracebase.opencode import collect
from tracebase.progress import LineProgressSink, ProgressReporter

SESSION = {
    "id": "ses_example",
    "time": {"created": 1767225600000, "updated": 1767225600000},
    "location": {"directory": "/workspace/example"},
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
        lambda args: json.dumps(next(responses)).encode(),
    )


def assert_unpublished(run):
    with pytest.raises(ArchiveError):
        result = collect(run, ProgressReporter(LineProgressSink(io.StringIO())))
        run.publish(result.coverage)
    assert not list((run.archive.root / "runs").glob("*"))


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
        lambda args: json.dumps(next(responses)).encode(),
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
