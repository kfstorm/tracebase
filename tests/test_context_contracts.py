import json
from pathlib import Path

import pytest

from tracebase import cli
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
) -> CollectionRun:
    return CollectionRun(
        archive,
        "opencode",
        "instance-1",
        CollectionRange.parse(from_text, to_text),
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
        / "observations"
        / "run-1"
        / "session.json"
    ).read_bytes() == b'{"timestamp":"2026-01-01T00:00:00.500000Z","body":"native"}'

    with pytest.raises(ContextError, match="already exists"):
        generate_context(archive, request, first)


def test_opencode_numeric_times_select_and_classify_grouped_observations(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    observations = (
        ("run-before", "2025-12-31T00:00:00Z", "2025-12-31T01:00:00Z", 1767139200000),
        ("run-in", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", 1767268800000),
        ("run-after", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", 1767312000000),
    )
    for run_id, start, end, created in observations:
        run = _run(archive, run_id, start, end)
        snapshot = run.write_snapshot(
            Snapshot(
                "opencode", "session", "same-session", {}, ({"path": "session.json"},)
            )
        )
        run.write_evidence(
            snapshot,
            "session.json",
            json.dumps(
                {"time": {"created": created, "updated": created}, "status": "done"}
            ).encode(),
        )
        run.publish({"observed": True})

    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    output = tmp_path / "output"
    generate_context(archive.root, request, output)
    item = json.loads((output / "context.json").read_text())["source_items"][0]
    assert item["temporal_roles"] == [
        "in_range_record",
        "prior_background",
        "later_development",
        "observed_state",
    ]
    assert {entry["run_id"] for entry in item["provenance"]} == {
        "run-before",
        "run-in",
        "run-after",
    }
    assert item["provenance"][0]["coverage"] == {"observed": True}
    assert (
        len(list((output / item["path"] / "observations").rglob("session.json"))) == 3
    )


def test_archive_rejects_unregistered_published_entries(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _archive_with_record(archive.root)
    run_root = next((archive.root / "runs").iterdir())
    (run_root / "unexpected.json").write_text("{}")
    with pytest.raises(ContextError, match="unregistered"):
        load_archive(archive.root)


def test_cli_structural_diagnostics_do_not_expose_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = tmp_path / "private-archive"

    def fail(*_args: object, **_kwargs: object) -> Path:
        raise OSError(f"cannot open {secret_path}")

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
