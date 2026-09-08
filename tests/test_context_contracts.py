import json
from pathlib import Path

import pytest

from tracebase.archive import Archive, CollectionRange, CollectionRun, Snapshot
from tracebase.context import ContextError, ContextRequest, generate_context


def _run(archive: Archive, run_id: str = "run-1") -> CollectionRun:
    return CollectionRun(
        archive,
        "opencode",
        "instance-1",
        CollectionRange.parse("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
        "test",
        {},
        run_id=run_id,
    )


def _archive_with_record(root: Path) -> None:
    archive = Archive(root)
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "session-1",
            {},
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(
        snapshot,
        "session.json",
        b'{"timestamp":"2026-01-01T00:00:00.500000Z","body":"native"}',
    )
    run.publish({})


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
    assert not (output / "github").exists()
    assert not (output / "opencode").exists()


def test_context_reads_current_archive_and_publishes_stable_source_bytes(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_record(archive)
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_context(archive, request, first)
    generate_context(archive, request, second)
    assert sorted(
        path.relative_to(first).as_posix()
        for path in first.rglob("*")
        if path.is_file()
    ) == sorted(
        path.relative_to(second).as_posix()
        for path in second.rglob("*")
        if path.is_file()
    )
    assert (first / "context.json").read_bytes() == (
        second / "context.json"
    ).read_bytes()
    assert (
        first
        / "opencode"
        / "aW5zdGFuY2UtMQ"
        / "session"
        / "c2Vzc2lvbi0x"
        / "session.json"
    ).read_bytes() == b'{"timestamp":"2026-01-01T00:00:00.500000Z","body":"native"}'

    with pytest.raises(ContextError, match="already exists"):
        generate_context(archive, request, first)
