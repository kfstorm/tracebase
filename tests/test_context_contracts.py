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
    evidence_path: str = "source.json",
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
    run.write_evidence(snapshot, evidence_path, b"{}")


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
    assert len(items) == 1
    assert [entry["run_id"] for entry in items[0]["provenance"]] == ["before", "in"]
    assert "inclusion_reasons" not in items[0]
    assert "temporal_roles" not in items[0]


def test_github_context_projects_native_records_without_fix_inference(
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
                "created_at": "2026-01-01T00:15:00Z",
                "user": {"login": "reviewer"},
            }
        ],
        "timeline.001.json": [
            {"id": 10, "event": "commented"},
            {"id": 20, "event": "reviewed", "actor": {"login": "reviewer"}},
            {"id": 40, "event": "closed", "actor": {"login": "closer"}},
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
        "pull-request.diff": "diff --git a/a b/a\n",
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
    target_snapshot = later.write_snapshot(
        Snapshot(
            "github",
            "issue",
            "ISSUE_99",
            later.collection_range.as_manifest(),
            ({"path": "issue.json"},),
        )
    )
    later.write_evidence(
        target_snapshot,
        "issue.json",
        json.dumps(
            {
                "node_id": "ISSUE_99",
                "created_at": "2025-01-01T00:00:00Z",
                "html_url": "https://github.com/example/repo/issues/99",
            }
        ).encode(),
    )
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
        "cross-referenced",
        "review-inline-comment",
        "thread-inline-comment",
    }
    assert projection["gaps"] == []
    assert next(
        record for record in projection["records"] if record["kind"] == "aggregate-diff"
    )["limitations"] == ["does_not_establish_fix_or_commit"]
    assert next(
        record
        for record in projection["records"]
        if record["kind"] == "ordinary-comment"
    )["temporal_roles"] == ["in_range_work"]
    ordinary_comment = next(
        record
        for record in projection["records"]
        if record["kind"] == "ordinary-comment"
    )
    assert len(ordinary_comment["representations"]) == 2
    review = next(
        record for record in projection["records"] if record["kind"] == "review"
    )
    assert len(review["representations"]) == 2
    lifecycle = next(
        record for record in projection["records"] if record["native_id"] == "40"
    )
    assert lifecycle["actor"] == {"login": "closer"}
    first_commit = next(
        record
        for record in projection["records"]
        if record["native_id"] == "commit-first"
    )
    assert first_commit["timestamps"] == {
        "author_date": "2025-12-31T23:00:00Z",
        "committer_date": "2026-01-01T01:00:00+01:00",
    }
    assert first_commit["temporal_roles"] == ["in_range_work"]
    record_ids = [record["native_id"] for record in projection["records"]]
    assert record_ids.index("commit-first") < record_ids.index("commit-second")
    pull_request = next(
        record for record in projection["records"] if record["kind"] == "pull-request"
    )
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
    assert manifest["unresolved_references"] == []
    references = [
        relation
        for relation in manifest["relations"]
        if relation["kind"] == "explicit-github-reference"
    ]
    assert len(references) == 3
    assert all(
        relation["url"] == "https://github.com/example/repo/issues/99"
        and relation["target_native_ids"] == ["ISSUE_99"]
        for relation in references
    )
    assert references[0]["occurrence_id"] != references[1]["occurrence_id"]
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
        evidence = next((run_root / "snapshots" / "session").iterdir()) / "source.json"
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
