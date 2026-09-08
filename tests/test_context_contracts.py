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
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(
        snapshot,
        "session.json",
        b'{"timestamp":"2026-01-01T00:00:00.500000Z","body":"native"}',
    )
    run.publish({})


def _snapshot_root(archive: Archive) -> Path:
    run_root = next((archive.root / "runs").iterdir())
    return next((run_root / "snapshots" / "session").iterdir())


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
        / "001-run-1"
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
        (
            "z-before",
            "2025-12-31T00:00:00Z",
            "2025-12-31T01:00:00Z",
            1767139200000,
            1767139200000,
        ),
        (
            "a-in",
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
            1767139200000,
            1767268800000,
        ),
        (
            "m-after",
            "2026-01-02T00:00:00Z",
            "2026-01-03T00:00:00Z",
            1767312000000,
            1767312000000,
        ),
    )
    for run_id, start, end, created, updated in observations:
        run = _run(archive, run_id, start, end)
        snapshot = run.write_snapshot(
            Snapshot(
                "opencode",
                "session",
                "same-session",
                {"from": start, "to": end},
                ({"path": "session.json"},),
            )
        )
        run.write_evidence(
            snapshot,
            "session.json",
            json.dumps(
                {
                    "role": "assistant",
                    "time": {"created": created, "updated": updated},
                    "status": "done",
                    "text": "done",
                }
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
    ]
    assert item["inclusion_reasons"] == [
        "source_record_in_range",
        "prior_background",
        "later_evidence",
    ]
    assert [entry["run_id"] for entry in item["provenance"]] == [
        "z-before",
        "a-in",
        "m-after",
    ]
    assert item["provenance"][0]["coverage"] == {"observed": True}
    assert "collector" in item["provenance"][0]
    assert "started_at" in item["provenance"][0]
    assert "completed_at" in item["provenance"][0]
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


def test_opencode_session_envelope_does_not_establish_work(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "envelope-only",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(
        snapshot,
        "session.json",
        b'{"time":{"created":1767225600000,"updated":1767229200000},"messages":[]}',
    )
    run.publish({})
    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    assert json.loads((output / "context.json").read_text())["source_items"] == []


def test_source_views_and_explicit_github_references_are_rendered(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    github = CollectionRun(
        archive,
        "github",
        "github-scope",
        CollectionRange.parse("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
        "test",
        {},
        run_id="github-run",
    )
    github_snapshot = github.write_snapshot(
        Snapshot(
            "github",
            "issue",
            "issue-native",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "issue.json"},),
        )
    )
    github.write_evidence(
        github_snapshot,
        "issue.json",
        b'{"node_id":"issue-native","html_url":"https://github.com/acme/project/issues/7",'
        b'"number":7,"body":"mentions https://github.com/acme/project/issues/9",'
        b'"updated_at":"2026-01-01T00:10:00Z"}',
    )
    github.publish({})
    repeated_github = CollectionRun(
        archive,
        "github",
        "github-scope",
        CollectionRange.parse("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
        "test",
        {},
        run_id="github-run-2",
    )
    repeated_snapshot = repeated_github.write_snapshot(
        Snapshot(
            "github",
            "issue",
            "issue-native",
            {"from": "2026-01-02T00:00:00Z", "to": "2026-01-02T01:00:00Z"},
            ({"path": "issue.json"},),
        )
    )
    repeated_github.write_evidence(
        repeated_snapshot,
        "issue.json",
        b'{"node_id":"issue-native","html_url":"https://github.com/acme/project/issues/7","number":7,"updated_at":"2026-01-02T00:10:00Z"}',
    )
    repeated_github.publish({})

    opencode = _run(archive, "opencode-run")
    session = opencode.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "reference-session",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "session.json"},),
        )
    )
    opencode.write_evidence(
        session,
        "session.json",
        json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "time": {"created": 1767226200000},
                        "text": (
                            "See https://github.com/acme/project/issues/7, "
                            "https://github.com/acme/project/pull/8, and "
                            "https://github.com/acme/project/issues/9"
                        ),
                    }
                ]
            }
        ).encode(),
    )
    opencode.publish({})

    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    manifest = json.loads((output / "context.json").read_text())
    opencode_item = next(
        item for item in manifest["source_items"] if item["source_kind"] == "opencode"
    )
    github_item = next(
        item for item in manifest["source_items"] if item["source_kind"] == "github"
    )
    assert (output / opencode_item["view_path"]).is_file()
    assert (output / github_item["view_path"]).is_file()
    assert github_item["view_path"] in (output / "index.md").read_text()
    github_view = (output / github_item["view_path"]).read_text()
    opencode_view = (output / opencode_item["view_path"]).read_text()
    assert "# GitHub Issue" in github_view
    assert "issue payload" in github_view
    assert "# OpenCode Session" in opencode_view
    assert len(manifest["relations"]) == 1
    assert manifest["relations"][0]["target"]["source_id"] == "issue-native"
    assert manifest["relations"][0]["occurrence"] == {
        "evidence_path": "session.json",
        "json_path": "$.messages[0].text",
        "offset": 4,
        "length": len("https://github.com/acme/project/issues/7"),
    }
    assert len(manifest["unresolved_references"]) == 2
    assert {reference["url"] for reference in manifest["unresolved_references"]} == {
        "https://github.com/acme/project/pull/8",
        "https://github.com/acme/project/issues/9",
    }
    index = (output / "index.md").read_text()
    assert "## Explicit References" in index
    assert "## Gaps" in index
    assert "session.json" in index


def test_snapshot_observation_and_encoded_path_identity_are_required(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _archive_with_record(archive.root)
    run_root = next((archive.root / "runs").iterdir())
    snapshot_entry = json.loads((run_root / "run.json").read_text())["snapshots"][0]
    snapshot_entry["path"] = "snapshots/session/not-the-source-id"
    run_manifest = json.loads((run_root / "run.json").read_text())
    run_manifest["snapshots"][0] = snapshot_entry
    (run_root / "run.json").write_text(json.dumps(run_manifest))
    with pytest.raises(ContextError, match="snapshot"):
        load_archive(archive.root)


def test_snapshot_observation_window_must_be_ordered_and_offset_aware(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _archive_with_record(archive.root)
    snapshot_root = _snapshot_root(archive)
    snapshot_manifest = json.loads((snapshot_root / "snapshot.json").read_text())
    snapshot_manifest["observation_window"] = {
        "from": "2026-01-01T01:00:00",
        "to": "2026-01-01T00:00:00Z",
    }
    (snapshot_root / "snapshot.json").write_text(json.dumps(snapshot_manifest))
    with pytest.raises(ContextError, match="observation window"):
        load_archive(archive.root)


def test_invalid_declared_json_and_unsupported_object_kind_fail_fast(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _archive_with_record(archive.root)
    snapshot_root = _snapshot_root(archive)
    (snapshot_root / "session.json").write_text("not json")
    with pytest.raises(ContextError, match="JSON evidence"):
        load_archive(archive.root)

    (snapshot_root / "session.json").write_text("{}")
    with pytest.raises(ContextError, match="unsupported source schema"):
        load_archive(archive.root)

    github_archive = Archive(tmp_path / "empty-github")
    github_archive.root.mkdir()
    github_run = CollectionRun(
        github_archive,
        "github",
        "github-scope",
        CollectionRange.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        "test",
        {},
        run_id="github-run",
    )
    github_snapshot = github_run.write_snapshot(
        Snapshot(
            "github",
            "issue",
            "empty-issue",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "issue.json"},),
        )
    )
    github_run.write_evidence(github_snapshot, "issue.json", b"{}")
    github_run.publish({})
    with pytest.raises(ContextError, match="unsupported source schema"):
        load_archive(github_archive.root)

    archive = Archive(tmp_path / "unsupported")
    archive.root.mkdir()
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "unsupported",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(snapshot, "session.json", b"{}")
    published = run.publish({})
    manifest_path = published / "run.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["snapshots"][0]["object_kind"] = "unsupported"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ContextError, match="snapshot"):
        load_archive(archive.root)


def test_required_collection_run_provenance_fields_are_validated(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    _archive_with_record(archive.root)
    run_root = next((archive.root / "runs").iterdir())
    manifest_path = run_root / "run.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["collector"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ContextError, match="collector"):
        load_archive(archive.root)


def test_symlinked_nested_evidence_parent_is_rejected(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "symlink-session",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "nested/session.json"},),
        )
    )
    run.write_evidence(
        snapshot,
        "nested/session.json",
        b'{"role":"assistant","timestamp":"2026-01-01T00:10:00Z","text":"work"}',
    )
    published = run.publish({})
    snapshot_root = published / "snapshots" / "session" / "c3ltbGluay1zZXNzaW9u"
    nested = snapshot_root / "nested"
    outside = tmp_path / "outside"
    outside.mkdir()
    (nested / "session.json").unlink()
    nested.rmdir()
    nested.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContextError, match="symlink"):
        load_archive(archive.root)


def test_source_gaps_preserve_explicit_truncation_uncertainty(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    run = _run(archive)
    snapshot = run.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "gap-session",
            {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T01:00:00Z"},
            ({"path": "session.json"},),
        )
    )
    run.write_evidence(
        snapshot,
        "session.json",
        b'{"messages":[{"role":"assistant","time":{"created":1767226200000},"truncated":true}]}',
    )
    run.publish({})
    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
        output,
    )
    gaps = json.loads((output / "context.json").read_text())["gaps"]
    assert {gap["kind"] for gap in gaps} == {
        "source_truncated",
    }


def test_context_refresh_does_not_retain_removed_archive_items(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_record(archive)
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    first = tmp_path / "first"
    generate_context(archive, request, first)
    assert json.loads((first / "context.json").read_text())["source_items"]

    shutil.rmtree(next((archive / "runs").iterdir()))
    second = tmp_path / "second"
    generate_context(archive, request, second)
    assert json.loads((second / "context.json").read_text())["source_items"] == []


def test_context_generation_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_urlopen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access is not allowed")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    archive = tmp_path / "archive"
    archive.mkdir()
    _archive_with_record(archive)
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
    _archive_with_record(archive)

    def fail_rename(self: Path, target: Path) -> Path:
        raise OSError("secret publication path")

    monkeypatch.setattr(Path, "rename", fail_rename)
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
    _archive_with_record(archive)

    def fail_write(path: Path, value: object) -> None:
        raise OSError("secret write path")

    def fail_cleanup(path: Path) -> None:
        raise OSError("secret cleanup path")

    monkeypatch.setattr(context_module, "_write_json", fail_write)
    monkeypatch.setattr(context_module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(ContextError, match="cleanup failed") as error:
        generate_context(
            archive,
            ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            tmp_path / "output",
        )
    assert str(error.value) == "context output cleanup failed"
