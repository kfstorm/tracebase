# ruff: noqa: E501

import base64
import json
import os
import subprocess
import sys
import tempfile
import uuid
from io import StringIO
from pathlib import Path

import pytest

from tracebase.archive import (
    Archive,
    ArchiveError,
    CollectionRange,
    CollectionRun,
    Snapshot,
    decode_path_id,
    encode_path_id,
    uuid7,
)
from tracebase.github import (
    _Candidate,
    _discover,
    _discovery_queries,
    _DiscoveryEntry,
    _GitHub,
    _hydrate,
    _Response,
    _updated_range,
    collect,
)
from tracebase.progress import LineProgressSink, ProgressEvent, ProgressReporter

PROJECT_ROOT = Path(__file__).parents[1]


class RecordingReporter:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)


def build_run(
    archive: Archive,
    from_text: str = "2026-01-01T00:00:00+00:00",
    to_text: str = "2026-01-01T01:00:00+00:00",
) -> CollectionRun:
    return CollectionRun(
        archive,
        "opencode",
        "instance-1",
        CollectionRange.parse(from_text, to_text),
        collector_version="test",
        effective_options={},
    )


def build_github_run(archive: Archive) -> CollectionRun:
    return CollectionRun(
        archive,
        "github",
        "actor-node",
        CollectionRange.parse("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        collector_version="test",
        effective_options={"actor_login": "actor"},
    )


def build_test_reporter() -> ProgressReporter:
    return ProgressReporter(LineProgressSink(StringIO()))


def build_github_pr_fixture() -> tuple[
    dict[str, object], dict[str, tuple[bytes, dict[str, str]]]
]:
    actor = {"node_id": "actor-node", "login": "actor"}
    item: dict[str, object] = {
        "node_id": "pr-node",
        "number": 7,
        "repository_url": "https://api.github.com/repos/octo/example",
        "pull_request": {},
    }
    responses: dict[str, tuple[bytes, dict[str, str]]] = {
        "/user": (json.dumps(actor).encode(), {}),
        "/repos/octo/example/issues/7": (
            json.dumps(
                {"node_id": "pr-node", "user": actor, "pull_request": {}}
            ).encode(),
            {},
        ),
        "/repos/octo/example/issues/7/comments?per_page=100&page=1": (b"[]", {}),
        "/repos/octo/example/issues/7/timeline?per_page=100&page=1": (b"[]", {}),
        "/repos/octo/example/pulls/7": (b'{"node_id":"pr-node"}', {}),
        "/repos/octo/example/pulls/7/reviews?per_page=100&page=1": (b"[]", {}),
        "/repos/octo/example/pulls/7/comments?per_page=100&page=1": (
            b'[{"id":101,"node_id":"comment-1","body":"rest first","path":"src/example.py","line":10,"original_line":10,"in_reply_to_id":null},{"id":102,"node_id":"comment-2","body":"rest second","path":"src/example.py","line":10,"original_line":10,"in_reply_to_id":101},{"id":103,"node_id":"comment-3","body":"rest third","path":"src/example.py","line":10,"original_line":10,"in_reply_to_id":102}]',
            {},
        ),
    }
    return item, responses


def build_github_request(
    item: dict[str, object], responses: dict[str, tuple[bytes, dict[str, str]]]
) -> object:
    def request(
        _self: object, endpoint: str, accept: str = "application/vnd.github+json"
    ) -> _Response:
        if endpoint.startswith("/search/issues?"):
            return _Response(
                json.dumps({"total_count": 1, "items": [item]}).encode(), 200, {}
            )
        body, headers = responses[endpoint]
        if accept == "application/vnd.github.diff":
            assert endpoint == "/repos/octo/example/pulls/7"
            return _Response(b"diff --git a/a b/a\n", 200, {})
        return _Response(body, 200, headers)

    return request


def github_response(status: int, body: bytes = b"{}") -> bytes:
    return f"HTTP/1.1 {status} Test\r\nX-Test: value\r\n\r\n".encode() + body


def test_github_request_retries_nonzero_cli_failure_without_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess(
                ["gh"], 1, stdout=b"", stderr=b"connection reset"
            ),
            subprocess.CompletedProcess(
                ["gh"], 0, stdout=github_response(200, b'{"ok":true}'), stderr=b""
            ),
        ]
    )
    delays: list[float] = []
    monkeypatch.setattr(
        "tracebase.github.subprocess.run", lambda *_args, **_kwargs: next(attempts)
    )
    monkeypatch.setattr("tracebase.github._sleep", delays.append)

    response = _GitHub().request("/user")

    assert response.status == 200
    assert response.body == b'{"ok":true}'
    assert delays == [0.5]


def test_github_request_does_not_retry_http_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            ["gh"],
            1,
            stdout=github_response(404, b'{"message":"Not Found"}'),
            stderr=b"not found",
        )

    monkeypatch.setattr("tracebase.github.subprocess.run", run)
    monkeypatch.setattr("tracebase.github._sleep", pytest.fail)

    with pytest.raises(ArchiveError, match="GitHub request failed"):
        _GitHub().request("/missing")

    assert calls == 1


def test_github_request_retries_http_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess(
                ["gh"],
                1,
                stdout=github_response(500, b"temporary"),
                stderr=b"server error",
            ),
            subprocess.CompletedProcess(
                ["gh"], 0, stdout=github_response(200, b"ok"), stderr=b""
            ),
        ]
    )
    delays: list[float] = []
    monkeypatch.setattr(
        "tracebase.github.subprocess.run", lambda *_args, **_kwargs: next(attempts)
    )
    monkeypatch.setattr("tracebase.github._sleep", delays.append)

    response = _GitHub().request("/user")

    assert response.status == 200
    assert delays == [0.5]


def test_github_request_reports_http_500_diagnostics_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tracebase.github.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["gh"],
            1,
            stdout=github_response(503, b"service unavailable"),
            stderr=b"server error",
        ),
    )
    delays: list[float] = []
    monkeypatch.setattr("tracebase.github._sleep", delays.append)

    with pytest.raises(
        ArchiveError,
        match=r"endpoint=/user.*attempts=3.*status=503.*stderr=<redacted bytes=12 sha256=[0-9a-f]+>.*body=<redacted bytes=19 sha256=[0-9a-f]+>",
    ) as error:
        _GitHub().request("/user")

    message = str(error.value)
    assert "server error" not in message
    assert "service unavailable" not in message

    assert delays == [0.5, 1.0]


def test_github_request_retries_timeout_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess(
                ["gh"], 0, stdout=github_response(200, b"ok"), stderr=b""
            )
        ]
    )
    timed_out = False

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal timed_out
        if not timed_out:
            timed_out = True
            raise subprocess.TimeoutExpired(["gh"], 60)
        return next(attempts)

    delays: list[float] = []
    monkeypatch.setattr("tracebase.github.subprocess.run", run)
    monkeypatch.setattr("tracebase.github._sleep", delays.append)

    response = _GitHub().request("/user")

    assert response.status == 200
    assert delays == [0.5]


def test_github_request_reports_transport_diagnostics_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tracebase.github.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["gh"], 7, stdout=b"", stderr=b"connection refused"
        ),
    )
    delays: list[float] = []
    monkeypatch.setattr("tracebase.github._sleep", delays.append)

    with pytest.raises(
        ArchiveError,
        match=r"endpoint=/user.*attempts=3.*returncode=7.*stderr=<redacted bytes=18 sha256=[0-9a-f]+>",
    ):
        _GitHub().request("/user")

    assert delays == [0.5, 1.0]


def test_github_graphql_uses_transport_retry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess(["gh"], 1, stdout=b"", stderr=b"network"),
            subprocess.CompletedProcess(
                ["gh"], 0, stdout=github_response(200, b'{"data":{}}'), stderr=b""
            ),
        ]
    )
    monkeypatch.setattr(
        "tracebase.github.subprocess.run", lambda *_args, **_kwargs: next(attempts)
    )
    monkeypatch.setattr("tracebase.github._sleep", lambda _delay: None)

    assert _GitHub().graphql("query { viewer { login } }").status == 200


def test_collection_run_snapshot_count_tracks_written_snapshots() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run = build_github_run(Archive(directory))

        assert run.snapshot_count == 0
        run.write_snapshot(
            Snapshot(
                source_kind="github",
                object_kind="issue",
                source_id="issue-node",
                observation_window={"from": "now", "to": "now"},
            )
        )

        assert run.snapshot_count == 1


@pytest.mark.parametrize(
    "endpoint",
    ["2026-01-01T00:00:00.1Z", "2026-01-01T00:00:00.000Z"],
)
def test_collection_range_rejects_fractional_seconds(endpoint: str) -> None:
    with pytest.raises(ArchiveError, match="whole seconds"):
        CollectionRange.parse(endpoint, "2026-01-01T01:00:00Z")


def test_github_discovery_uses_one_updated_range_qualifier() -> None:
    collection_range = CollectionRange.parse(
        "2026-09-06T00:00:00+08:00", "2026-09-07T00:00:00+08:00"
    )

    queries = _discovery_queries("actor", collection_range)

    assert len(queries) == 3
    assert all(query.count("updated:") == 1 for _reason, query in queries)
    assert all(
        "updated:2026-09-06T00:00:00+08:00..2026-09-06T23:59:59+08:00" in query
        for _reason, query in queries
    )


def test_updated_range_maps_half_open_range_to_inclusive_search_range() -> None:
    collection_range = CollectionRange.parse(
        "2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z"
    )

    assert (
        _updated_range(collection_range.start, collection_range.end)
        == "updated:2026-01-01T00:00:00Z..2026-01-01T00:00:02Z"
    )


def run_github_cli(
    archive: Path,
    fixture_directory: Path,
    from_text: str = "2026-01-01T00:00:00+00:00",
    to_text: str = "2026-01-02T00:00:00+00:00",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tracebase",
            "collect",
            "github",
            "--archive",
            str(archive),
            "--from",
            from_text,
            "--to",
            to_text,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ
        | {"PATH": str(fixture_directory) + os.pathsep + os.environ["PATH"]}
        | (extra_environment or {}),
    )


def write_failure_gh(fixture_directory: Path, mode: str) -> None:
    (fixture_directory / "gh").write_text(
        f"""#!/usr/bin/env python3
import json
import sys

endpoint = sys.argv[-1]
if endpoint == "--help":
    print("gh api help")
    raise SystemExit(0)
if endpoint == "/user":
    body = b'{{"node_id":"actor-node","login":"actor"}}'
elif {mode!r} == "unresolved" and endpoint.startswith("/search/issues?"):
    body = b'{{"total_count":1001,"incomplete_results":false,"items":[]}}'
elif {mode!r} == "later-page" and endpoint.endswith("page=1"):
    body = b'{{"total_count":1,"incomplete_results":false,"items":[]}}'
    headers = b"HTTP/1.1 200 OK\\r\\nLink: <next>; rel=\\\"next\\\"\\r\\n\\r\\n"
    sys.stdout.buffer.write(headers + body)
    raise SystemExit(0)
elif {mode!r} == "later-page" and endpoint.endswith("page=2"):
    body = b'{{"total_count":1,"incomplete_results":true,"items":[]}}'
elif {mode!r} == "count-mismatch" and endpoint.startswith("/search/issues?"):
    items = [{{"node_id": str(index), "repository_url": "https://api.github.com/repos/octo/example", "number": index}} for index in range(100)]
    body = json.dumps({{"total_count": 150, "incomplete_results": False, "items": items}}).encode()
elif {mode!r} == "duplicate-count" and endpoint.endswith("page=1"):
    items = [{{"node_id": str(index), "repository_url": "https://api.github.com/repos/octo/example", "number": index}} for index in range(100)]
    body = json.dumps({{"total_count": 150, "incomplete_results": False, "items": items}}).encode()
    headers = b"HTTP/1.1 200 OK\\r\\nLink: <next>; rel=\\\"next\\\"\\r\\n\\r\\n"
    sys.stdout.buffer.write(headers + body)
    raise SystemExit(0)
elif {mode!r} == "duplicate-count" and endpoint.endswith("page=2"):
    items = [{{"node_id": str(index), "repository_url": "https://api.github.com/repos/octo/example", "number": index}} for index in range(130)]
    body = json.dumps({{"total_count": 150, "incomplete_results": False, "items": items[0:20] + items[100:130]}}).encode()
else:
    raise SystemExit(1)
sys.stdout.buffer.write(b"HTTP/1.1 200 OK\\r\\n\\r\\n" + body)
""",
        encoding="utf-8",
    )
    (fixture_directory / "gh").chmod(0o755)


class TestCollectionCli:
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tracebase", *arguments],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    @pytest.mark.parametrize("source", ["github", "opencode"])
    def test_fractional_range_fails_before_staging(self, source: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = [
                "collect",
                source,
                "--archive",
                directory,
                "--from",
                "2026-01-01T00:00:00.1Z",
                "--to",
                "2026-01-01T01:00:00Z",
            ]
            if source == "opencode":
                arguments.extend(["--instance-id", "instance-1"])

            result = self.run_cli(*arguments)

            assert result.returncode == 1
            assert result.stdout == ""
            assert "whole seconds" in result.stderr
            assert not (Path(directory) / ".staging").exists()

    def test_invalid_range_fails_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "collect",
                "opencode",
                "--archive",
                directory,
                "--instance-id",
                "instance-1",
                "--from",
                "2026-01-01T00:00:00",
                "--to",
                "2026-01-01T01:00:00+00:00",
            )

            assert result.returncode == 1
            assert result.stdout == ""
            assert "explicit offset" in result.stderr
            assert not (Path(directory) / ".staging").exists()

    def test_unsupported_option_does_not_echo_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "authorization-secret"
            result = self.run_cli(
                "collect",
                "github",
                "--archive",
                directory,
                "--from",
                "2026-01-01T00:00:00+00:00",
                "--to",
                "2026-01-01T01:00:00+00:00",
                "--token",
                secret,
            )

            assert result.returncode == 1
            assert secret not in result.stderr
            assert secret not in result.stdout

    def test_option_abbreviation_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "collect",
                "opencode",
                "--arch",
                directory,
                "--instance-id",
                "instance-1",
                "--from",
                "2026-01-01T00:00:00+00:00",
                "--to",
                "2026-01-01T01:00:00+00:00",
            )

            assert result.returncode == 1
            assert result.stdout == ""
            assert "invalid command arguments" in result.stderr
            assert not (Path(directory) / ".staging").exists()

    @pytest.mark.parametrize(
        ("from_text", "to_text"),
        [
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            ("2026-01-01T01:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        ],
    )
    def test_equal_or_reversed_range_fails_before_staging(
        self, from_text: str, to_text: str
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "collect",
                "opencode",
                "--archive",
                directory,
                "--instance-id",
                "instance-1",
                "--from",
                from_text,
                "--to",
                to_text,
            )

            assert result.returncode == 1
            assert result.stdout == ""
            assert "before to" in result.stderr
            assert not (Path(directory) / ".staging").exists()

    def test_published_overlap_is_rejected_without_new_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(directory)
            run = build_run(archive)
            run.publish({})

            result = self.run_cli(
                "collect",
                "opencode",
                "--archive",
                directory,
                "--instance-id",
                "instance-1",
                "--from",
                "2026-01-01T00:30:00+00:00",
                "--to",
                "2026-01-01T01:30:00+00:00",
            )

            assert result.returncode == 1
            assert "overlaps" in result.stderr
            assert list((Path(directory) / ".staging").iterdir()) == []


def test_path_id_is_reversible_base64url_without_padding() -> None:
    source_id = "repo/issue:42?秘密"

    path_id = encode_path_id(source_id)

    assert path_id == base64.urlsafe_b64encode(source_id.encode()).decode().rstrip("=")
    assert decode_path_id(path_id) == source_id
    assert "=" not in path_id


def test_uuid7_has_uuid7_version_and_rfc_variant() -> None:
    value = uuid7()

    assert isinstance(value, uuid.UUID)
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_collection_run_publishes_empty_run_and_snapshot_manifest() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        run = build_run(archive)
        published = run.publish({"observed": True})

        manifest = json.loads((published / "run.json").read_text())
        assert manifest["run_id"] == run.run_id
        assert manifest["source"] == {"kind": "opencode", "scope_id": "instance-1"}
        assert manifest["collection_range"] == {
            "from": "2026-01-01T00:00:00+00:00",
            "to": "2026-01-01T01:00:00+00:00",
        }
        assert manifest["snapshots"] == []
        assert (published / "snapshots").is_dir()
        assert not run.staging.exists()

        next_run = build_run(
            archive,
            "2026-01-01T01:00:00+00:00",
            "2026-01-01T02:00:00+00:00",
        )
        snapshot_root = next_run.write_snapshot(
            Snapshot(
                source_kind="opencode",
                object_kind="session",
                source_id="session/id:unsafe",
                observation_window={
                    "from": "2026-01-01T01:00:00+00:00",
                    "to": "2026-01-01T01:30:00+00:00",
                },
                evidence_files=({"path": "session.json"},),
            )
        )
        next_run.write_evidence(snapshot_root, "session.json", b"source-native")
        published = next_run.publish({"observed": True})

        snapshot_directory = published / "snapshots/session/c2Vzc2lvbi9pZDp1bnNhZmU"
        snapshot_manifest = json.loads(
            (snapshot_directory / "snapshot.json").read_text()
        )
        assert snapshot_manifest["source_kind"] == "opencode"
        assert snapshot_manifest["object_kind"] == "session"
        assert snapshot_manifest["source_id"] == "session/id:unsafe"
        assert snapshot_manifest["evidence_files"] == [{"path": "session.json"}]
        assert (snapshot_directory / "session.json").read_bytes() == b"source-native"
        assert manifest["snapshots"] == []
        next_manifest = json.loads(
            (published.parent / next_run.run_id / "run.json").read_text()
        )
        assert next_manifest["snapshots"] == [
            {
                "object_kind": "session",
                "path": "snapshots/session/c2Vzc2lvbi9pZDp1bnNhZmU",
                "source_id": "session/id:unsafe",
                "source_kind": "opencode",
            }
        ]


def test_publish_rejects_missing_declared_evidence_and_keeps_staging() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        run = build_run(archive)
        run.write_snapshot(
            Snapshot(
                source_kind="opencode",
                object_kind="session",
                source_id="session-1",
                observation_window={},
                evidence_files=({"path": "session.json"},),
            )
        )

        with pytest.raises(ArchiveError, match="evidence"):
            run.publish({})

        assert run.staging.exists()
        assert not (Path(directory) / "runs").exists()


def test_publish_rejects_unlisted_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        run = build_run(archive)
        snapshot_root = run.write_snapshot(
            Snapshot(
                source_kind="opencode",
                object_kind="session",
                source_id="session-1",
                observation_window={},
                evidence_files=({"path": "session.json"},),
            )
        )
        run.write_evidence(snapshot_root, "session.json", b"session")
        run.write_evidence(snapshot_root, "unexpected.json", b"unexpected")

        with pytest.raises(ArchiveError, match="evidence"):
            run.publish({})

        assert run.staging.exists()
        assert not (Path(directory) / "runs").exists()


def test_write_snapshot_rejects_non_json_metadata_before_creating_files() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run = build_run(Archive(directory))

        with pytest.raises(ArchiveError, match="metadata"):
            run.write_snapshot(
                Snapshot(
                    source_kind="opencode",
                    object_kind="session",
                    source_id="session-1",
                    observation_window={},
                    metadata={"invalid": {1: "numeric", "1": "text"}},
                )
            )

        assert list((run.staging / "snapshots").iterdir()) == []


def test_manifest_paths_are_posix_and_object_kind_is_an_archive_identifier() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        run = build_run(archive)
        with pytest.raises(ArchiveError, match="object kind"):
            run.write_snapshot(
                Snapshot(
                    source_kind="opencode",
                    object_kind="../session",
                    source_id="session-1",
                    observation_window={},
                )
            )

        snapshot_root = run.write_snapshot(
            Snapshot(
                source_kind="opencode",
                object_kind="session",
                source_id="session-2",
                observation_window={},
                evidence_files=({"path": "nested/session.json"},),
            )
        )
        run.write_evidence(snapshot_root, Path("nested") / "session.json", b"session")
        published = run.publish({})
        snapshot_directory = published / "snapshots/session/c2Vzc2lvbi0y"
        snapshot_manifest = json.loads(
            (snapshot_directory / "snapshot.json").read_text()
        )

        assert snapshot_manifest["evidence_files"] == [{"path": "nested/session.json"}]
        assert (
            json.loads((published / "run.json").read_text())["snapshots"][0]["path"]
            == "snapshots/session/c2Vzc2lvbi0y"
        )


def test_symlinked_snapshot_area_cannot_escape_staging() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        run = build_run(archive)
        outside = Path(directory).parent / f"{Path(directory).name}-outside"
        outside.mkdir()
        try:
            snapshot_area = run.staging / "snapshots"
            snapshot_area.rmdir()
            snapshot_area.symlink_to(outside, target_is_directory=True)

            with pytest.raises(ArchiveError, match="symlink"):
                run.write_snapshot(
                    Snapshot(
                        source_kind="opencode",
                        object_kind="session",
                        source_id="session-1",
                        observation_window={},
                    )
                )
        finally:
            snapshot_area.unlink(missing_ok=True)
            outside.rmdir()


def test_archive_root_symlink_is_caller_boundary() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.mkdir()
        archive_root = Path(directory) / "archive"
        archive_root.symlink_to(target, target_is_directory=True)

        run = build_run(Archive(archive_root))

        assert run.staging.is_dir()


def test_overlap_registry_uses_published_runs_and_half_open_ranges() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        run = build_run(archive)
        run.publish({})

        assert archive.has_overlap(
            "opencode",
            "instance-1",
            CollectionRange.parse(
                "2026-01-01T00:30:00+00:00", "2026-01-01T01:30:00+00:00"
            ),
        )
        assert not archive.has_overlap(
            "opencode",
            "instance-1",
            CollectionRange.parse(
                "2026-01-01T01:00:00+00:00", "2026-01-01T02:00:00+00:00"
            ),
        )
        assert not archive.has_overlap(
            "opencode",
            "other-instance",
            CollectionRange.parse(
                "2026-01-01T00:30:00+00:00", "2026-01-01T01:30:00+00:00"
            ),
        )


def test_publish_rechecks_overlap_for_runs_staged_before_another_publish() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        first = build_run(archive)
        second = build_run(archive)

        first.publish({})
        with pytest.raises(ValueError, match="overlaps"):
            second.publish({})

        assert second.staging.exists()
        assert not (Path(directory) / "runs" / second.run_id).exists()


def test_github_collect_hydrates_paginated_pr_with_source_native_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, responses = build_github_pr_fixture()
    responses.update(
        {
            "/repos/octo/example/issues/7/comments?per_page=100&page=1": (
                b'[{"user":{"node_id":"actor-node"}}]',
                {"link": '<next>; rel="next"'},
            ),
            "/repos/octo/example/issues/7/comments?per_page=100&page=2": (
                b"[]",
                {},
            ),
            "/repos/octo/example/pulls/7/reviews?per_page=100&page=1": (
                b'[{"user":{"node_id":"actor-node"},"submitted_at":"2026-01-01T00:00:00Z"}]',
                {},
            ),
        }
    )

    request = build_github_request(item, responses)

    def graphql(_self: object, _query: str) -> _Response:
        return _Response(
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            ).encode(),
            200,
            {},
        )

    monkeypatch.setattr("tracebase.github._GitHub.request", request)
    monkeypatch.setattr("tracebase.github._GitHub.graphql", graphql)
    with tempfile.TemporaryDirectory() as directory:
        run = build_github_run(Archive(directory))
        reporter = RecordingReporter()

        result = collect(run, reporter)
        published = run.publish(result.coverage)
        snapshot = next((published / "snapshots/pull-request").iterdir())
        manifest = json.loads((snapshot / "snapshot.json").read_text())

        assert run.snapshot_count == 1
        assert result.coverage["selected_artifacts"] == 1
        assert result.coverage["discovery_matrix_version"] == 1
        assert [entry["reason"] for entry in result.coverage["queries"]] == [
            "authorship",
            "ordinary_comment",
            "submitted_review",
        ]
        assert manifest["selection_provenance"][0]["eligibility"] == [
            "authorship",
            "ordinary_comment",
            "submitted_review",
        ]
        assert {
            entry["reason"]
            for entry in manifest["selection_provenance"][0]["discovery_query_entries"]
        } == {"authorship", "ordinary_comment", "submitted_review"}
        assert all(
            entry["partition"]
            == {
                "from": "2026-01-01T00:00:00Z",
                "to": "2026-01-02T00:00:00Z",
            }
            for entry in manifest["selection_provenance"][0]["discovery_query_entries"]
        )
        assert (snapshot / "comments.002.json").read_bytes() == b"[]"
        assert (snapshot / "timeline.001.json").exists()
        assert (snapshot / "reviews.001.json").exists()
        assert (snapshot / "review-comments.001.json").exists()
        assert (snapshot / "pull-request.diff").read_bytes() == b"diff --git a/a b/a\n"
        assert (
            json.loads((published / "run.json").read_text())["snapshots"][0][
                "source_kind"
            ]
            == "github"
        )
        assert [(event.task_id, event.kind) for event in reporter.events] == [
            ("github.discover", "start"),
            ("github.discover", "update"),
            ("github.discover", "update"),
            ("github.discover", "update"),
            ("github.discover", "finish"),
            ("github.hydrate", "start"),
            ("github.hydrate", "update"),
            ("github.hydrate", "update"),
            ("github.hydrate", "finish"),
        ]
        assert reporter.events[1].message == "authorship: page 1, 1 candidates"
        assert reporter.events[6].total == 1
        assert reporter.events[6].current == "octo/example#7"
        assert reporter.events[6].phase == "starting"
        assert reporter.events[7].phase == "completed"


def test_github_collect_preserves_review_thread_source_native_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, responses = build_github_pr_fixture()
    thread_page_one = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread-resolved-current",
                                    "path": "src/example.py",
                                    "line": 10,
                                    "originalLine": 10,
                                    "startLine": 8,
                                    "originalStartLine": 8,
                                    "diffSide": "RIGHT",
                                    "startDiffSide": "RIGHT",
                                    "isResolved": True,
                                    "isOutdated": False,
                                    "resolvedBy": {
                                        "id": "reviewer-id",
                                        "login": "reviewer",
                                    },
                                    "comments": {
                                        "nodes": [
                                            {"id": "comment-1"},
                                            {"id": "comment-2"},
                                        ],
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "comment-cursor-1",
                                        },
                                    },
                                },
                                {
                                    "id": "thread-unresolved-outdated",
                                    "path": "src/old.py",
                                    "line": None,
                                    "originalLine": 20,
                                    "startLine": None,
                                    "originalStartLine": 20,
                                    "diffSide": "RIGHT",
                                    "startDiffSide": "RIGHT",
                                    "isResolved": False,
                                    "isOutdated": True,
                                    "resolvedBy": None,
                                    "comments": {
                                        "nodes": [{"id": "comment-3"}],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    },
                                },
                            ],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "thread-cursor-1",
                            },
                        }
                    }
                }
            }
        },
        separators=(",", ":"),
    ).encode()
    nested_comment_page = b'{"data":{"node":{"id":"thread-resolved-current","comments":{"nodes":[{"id":"comment-3"}],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}'
    thread_page_two = b'{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}}'
    outer_graphql_responses = iter(
        [
            _Response(thread_page_one, 200, {}),
            _Response(thread_page_two, 200, {}),
        ]
    )

    request = build_github_request(item, responses)

    def graphql(_self: object, query: str) -> _Response:
        assert "body" not in query
        assert "databaseId" not in query
        assert "diffHunk" not in query
        if "PullRequestReviewThreadComments" in query:
            return _Response(nested_comment_page, 200, {})
        assert "reviewThreads" in query
        return next(outer_graphql_responses)

    monkeypatch.setattr("tracebase.github._GitHub.request", request)
    monkeypatch.setattr("tracebase.github._GitHub.graphql", graphql)
    with tempfile.TemporaryDirectory() as directory:
        run = build_github_run(Archive(directory))

        result = collect(run, build_test_reporter())
        _hydrate(
            run,
            _GitHub(),
            _Candidate(
                "pr-node",
                "octo/example",
                7,
                "pull-request",
                (),
            ),
        )
        assert run.snapshot_count == 1
        published = run.publish(result.coverage)
        snapshot = next((published / "snapshots/pull-request").iterdir())
        manifest = json.loads((snapshot / "snapshot.json").read_text())

        assert (snapshot / "review-threads.001.json").read_bytes() == thread_page_one
        assert (snapshot / "review-threads.002.json").read_bytes() == thread_page_two
        assert (
            snapshot / "review-thread-comments.001.002.json"
        ).read_bytes() == nested_comment_page
        thread_entries = [
            entry
            for entry in manifest["evidence_files"]
            if entry["path"].startswith("review-threads.")
        ]
        assert [entry["path"] for entry in thread_entries] == [
            "review-threads.001.json",
            "review-threads.002.json",
        ]
        assert all(entry["request"]["method"] == "POST" for entry in thread_entries)
        assert all(
            entry["request"]["api"] == "GitHub GraphQL API" for entry in thread_entries
        )
        assert all("GH_TOKEN" not in json.dumps(entry) for entry in thread_entries)
        supplemental_entry = next(
            entry
            for entry in manifest["evidence_files"]
            if entry["path"] == "review-thread-comments.001.002.json"
        )
        assert supplemental_entry["request"]["method"] == "POST"
        assert json.loads(nested_comment_page)["data"]["node"]["id"] == (
            "thread-resolved-current"
        )
        rest_comments = json.loads((snapshot / "review-comments.001.json").read_bytes())
        first_thread = json.loads((snapshot / "review-threads.001.json").read_bytes())[
            "data"
        ]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]
        supplemental_comments = json.loads(
            (snapshot / "review-thread-comments.001.002.json").read_bytes()
        )["data"]["node"]["comments"]["nodes"]
        graph_comment_ids = {
            comment["id"] for comment in first_thread["comments"]["nodes"]
        } | {comment["id"] for comment in supplemental_comments}
        assert graph_comment_ids == {comment["node_id"] for comment in rest_comments}
        assert first_thread["comments"]["nodes"] == [
            {"id": "comment-1"},
            {"id": "comment-2"},
        ]
        threads = json.loads((snapshot / "review-threads.001.json").read_bytes())[
            "data"
        ]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        assert [
            (
                thread["id"],
                thread["path"],
                thread["line"],
                thread["originalLine"],
                thread["startLine"],
                thread["originalStartLine"],
                thread["isResolved"],
                thread["isOutdated"],
                thread["resolvedBy"],
            )
            for thread in threads
        ] == [
            (
                "thread-resolved-current",
                "src/example.py",
                10,
                10,
                8,
                8,
                True,
                False,
                {"id": "reviewer-id", "login": "reviewer"},
            ),
            (
                "thread-unresolved-outdated",
                "src/old.py",
                None,
                20,
                None,
                20,
                False,
                True,
                None,
            ),
        ]


@pytest.mark.parametrize("mode", ["retry", "exhausted"])
def test_github_hydration_enforces_review_comment_join(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    item, responses = build_github_pr_fixture()
    review_comments_endpoint = (
        "/repos/octo/example/pulls/7/comments?per_page=100&page=1"
    )
    full_review_comments = responses[review_comments_endpoint][0]
    incomplete_review_comments = json.dumps(
        json.loads(full_review_comments)[:2], separators=(",", ":")
    ).encode()
    graphql_response = _Response(
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "thread-1",
                                        "comments": {
                                            "nodes": [
                                                {"id": "comment-1"},
                                                {"id": "comment-2"},
                                                {"id": "comment-3"},
                                            ],
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        },
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                }
            },
            separators=(",", ":"),
        ).encode(),
        200,
        {},
    )
    base_request = build_github_request(item, responses)
    review_comment_calls = 0

    def request(
        self: object, endpoint: str, accept: str = "application/vnd.github+json"
    ) -> _Response:
        nonlocal review_comment_calls
        if endpoint == review_comments_endpoint:
            review_comment_calls += 1
            if mode == "exhausted" or review_comment_calls == 1:
                return _Response(incomplete_review_comments, 200, {})
            return _Response(full_review_comments, 200, {})
        return base_request(self, endpoint, accept)

    def graphql(_self: object, _query: str) -> _Response:
        return graphql_response

    monkeypatch.setattr("tracebase.github._GitHub.request", request)
    monkeypatch.setattr("tracebase.github._GitHub.graphql", graphql)
    with tempfile.TemporaryDirectory() as directory:
        run = build_github_run(Archive(directory))
        if mode == "retry":
            result = collect(run, build_test_reporter())
            published = run.publish(result.coverage)
            snapshot = next((published / "snapshots/pull-request").iterdir())

            assert review_comment_calls == 2
            assert (
                snapshot / "review-comments.001.json"
            ).read_bytes() == full_review_comments
        else:
            with pytest.raises(ArchiveError, match="review comment join"):
                collect(run, build_test_reporter())

            assert review_comment_calls == 3
            assert run.staging.exists()
            assert not (Path(directory) / "runs").exists()


def test_github_collect_failure_keeps_run_unpublished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request(_self: object, _endpoint: str, _accept: str = "") -> _Response:
        raise ArchiveError("GitHub request failed")

    monkeypatch.setattr("tracebase.github._GitHub.request", request)
    with tempfile.TemporaryDirectory() as directory:
        run = build_github_run(Archive(directory))

        with pytest.raises(ArchiveError, match="request failed"):
            collect(run, build_test_reporter())

        assert run.staging.exists()
        assert not (Path(directory) / "runs").exists()


def test_github_hydration_keeps_discovery_selection_when_payload_disagrees() -> None:
    class HydrationFixture(_GitHub):
        def request(self, endpoint: str, _accept: str = "") -> _Response:
            if endpoint.endswith("/issues/1"):
                body = b'{"node_id":"issue-node","user":{"node_id":"other"}}'
            else:
                body = b"[]"
            return _Response(body, 200, {})

    with tempfile.TemporaryDirectory() as directory:
        run = build_github_run(Archive(directory))
        _hydrate(
            run,
            HydrationFixture(),
            _Candidate(
                "issue-node",
                "octo/example",
                1,
                "issue",
                (
                    _DiscoveryEntry(
                        "authorship",
                        "author:actor updated:>=x updated:<y",
                        "x",
                        "y",
                    ),
                ),
            ),
        )

        published = run.publish({})
        snapshot = next((published / "snapshots/issue").iterdir())
        assert json.loads((snapshot / "snapshot.json").read_text())[
            "selection_provenance"
        ][0]["eligibility"] == ["authorship"]


@pytest.mark.parametrize("fail", [False, True])
def test_github_cli_uses_synthetic_gh_and_publishes_atomically(
    fail: bool, tmp_path: Path
) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        f"""#!/usr/bin/env python3
import json
import sys

endpoint = sys.argv[-1]
if endpoint == "--help":
    print("gh api help")
    raise SystemExit(0)
if {fail!r}:
    raise SystemExit(1)
if endpoint == "/user":
    body = json.dumps({{"node_id": "actor-node", "login": "actor"}}).encode()
elif endpoint.startswith("/search/issues?"):
    forbidden = ("mentions%3A", "assignee%3A", "review-requested%3A", "involves%3A", "commits%3A", "commit%3A")
    if any(term in endpoint for term in forbidden):
        raise SystemExit(1)
    body = b'{{"total_count":0,"incomplete_results":false,"items":[]}}'
else:
    raise SystemExit(2)
headers = b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n"
sys.stdout.buffer.write(headers + body)
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    archive = tmp_path / "archive"

    result = run_github_cli(archive, tmp_path)

    if fail:
        assert result.returncode == 1
        assert not (archive / "runs").exists()
    else:
        assert result.returncode == 0
        run = next((archive / "runs").iterdir())
        manifest = json.loads((run / "run.json").read_text())
        assert manifest["source"] == {"kind": "github", "scope_id": "actor-node"}
        assert manifest["coverage"]["discovery_matrix_version"] == 1
        assert manifest["snapshots"] == []
        assert [entry["reason"] for entry in manifest["coverage"]["queries"]] == [
            "authorship",
            "ordinary_comment",
            "submitted_review",
        ]


@pytest.mark.parametrize("capability", ["legacy", "modern"])
def test_github_cli_hydrates_eligible_pr_from_synthetic_gh(
    capability: str, tmp_path: Path
) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

endpoint = sys.argv[-1]
capability = os.environ["TRACEBASE_FIXTURE_CAPABILITY"]
if endpoint == "--help":
    print("gh api help" + (" --allow-escape-sequences" if capability == "modern" else ""))
    raise SystemExit(0)
has_escape_flag = "--allow-escape-sequences" in sys.argv
is_diff = any("application/vnd.github.diff" in arg for arg in sys.argv)
if has_escape_flag != (capability == "modern" and is_diff):
    raise SystemExit(1)
assert "X-GitHub-Api-Version: 2022-11-28" in sys.argv
headers = b"HTTP/1.1 200 OK\\r\\n\\r\\n"
item = {"node_id": "pr-node", "number": 7, "repository_url": "https://api.github.com/repos/octo/example", "pull_request": {}}
def review_thread(thread_id, resolved, outdated, line, original_line, resolver):
    return {
        "id": thread_id,
        "path": "src/example.py",
        "line": line,
        "originalLine": original_line,
        "startLine": line,
        "originalStartLine": original_line,
        "diffSide": "RIGHT",
        "startDiffSide": "RIGHT",
        "isResolved": resolved,
        "isOutdated": outdated,
        "resolvedBy": {"id": "reviewer-id", "login": "reviewer"} if resolver else None,
        "comments": {
            "nodes": [
                {"id": thread_id + "-comment-1"},
                {"id": thread_id + "-comment-2"},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }
if endpoint == "/user": body = b'{"node_id":"actor-node","login":"actor"}'
elif endpoint.startswith("/search/issues?"): body = json.dumps({"total_count": 1, "incomplete_results": False, "items": [item]}).encode()
elif endpoint == "/repos/octo/example/issues/7": body = b'{"node_id":"pr-node","user":{"node_id":"actor-node"},"pull_request":{}}'
elif endpoint.endswith("/issues/7/comments?per_page=100&page=1"):
    body = b'[{"user":{"node_id":"actor-node"}}]'
    headers = b"HTTP/1.1 200 OK\\r\\nLink: <next>; rel=\\\"next\\\"\\r\\n\\r\\n"
elif endpoint.endswith("/issues/7/comments?per_page=100&page=2"): body = b"[]"
elif endpoint.endswith("/issues/7/timeline?per_page=100&page=1"): body = b"[]"
elif endpoint.endswith("/pulls/7/comments?per_page=100&page=1"):
    body = json.dumps([
        {"node_id": thread_id + "-comment-" + str(number)}
        for thread_id in (
            "thread-cli-resolved-current",
            "thread-cli-unresolved-current",
            "thread-cli-resolved-outdated",
            "thread-cli-unresolved-outdated",
        )
        for number in (1, 2)
    ]).encode()
elif endpoint.endswith("/pulls/7/reviews?per_page=100&page=1"): body = b'[{"user":{"node_id":"actor-node"},"submitted_at":"x"}]'
elif endpoint.startswith("query=query PullRequestReviewThreads"):
    body = json.dumps({
        "data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [
                review_thread("thread-cli-resolved-current", True, False, 4, 4, True),
                review_thread("thread-cli-unresolved-current", False, False, 8, 8, False),
                review_thread("thread-cli-resolved-outdated", True, True, 12, 12, True),
                review_thread("thread-cli-unresolved-outdated", False, True, None, 16, False),
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}
    }).encode()
elif endpoint == "/repos/octo/example/pulls/7" and any("application/vnd.github.diff" in arg for arg in sys.argv): body = b"diff --git a/a b/a\\n"
elif endpoint == "/repos/octo/example/pulls/7": body = b'{"node_id":"pr-node"}'
else: raise SystemExit(2)
sys.stdout.buffer.write(headers + body)
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    archive = tmp_path / "archive"
    result = run_github_cli(
        archive,
        tmp_path,
        extra_environment={"TRACEBASE_FIXTURE_CAPABILITY": capability},
    )
    assert result.returncode == 0
    run = next((archive / "runs").iterdir())
    snapshot = next((run / "snapshots/pull-request").iterdir())
    assert (snapshot / "comments.002.json").read_bytes() == b"[]"
    threads = json.loads((snapshot / "review-threads.001.json").read_bytes())["data"][
        "repository"
    ]["pullRequest"]["reviewThreads"]["nodes"]
    assert [
        (
            thread["id"],
            thread["isResolved"],
            thread["isOutdated"],
            thread["line"],
            thread["originalLine"],
            thread["resolvedBy"],
        )
        for thread in threads
    ] == [
        (
            "thread-cli-resolved-current",
            True,
            False,
            4,
            4,
            {"id": "reviewer-id", "login": "reviewer"},
        ),
        ("thread-cli-unresolved-current", False, False, 8, 8, None),
        (
            "thread-cli-resolved-outdated",
            True,
            True,
            12,
            12,
            {"id": "reviewer-id", "login": "reviewer"},
        ),
        ("thread-cli-unresolved-outdated", False, True, None, 16, None),
    ]
    assert [comment["id"] for comment in threads[0]["comments"]["nodes"]] == [
        "thread-cli-resolved-current-comment-1",
        "thread-cli-resolved-current-comment-2",
    ]
    assert (snapshot / "pull-request.diff").read_bytes() == b"diff --git a/a b/a\n"
    assert json.loads((snapshot / "snapshot.json").read_text())["selection_provenance"][
        0
    ]["eligibility"] == ["authorship", "ordinary_comment", "submitted_review"]


@pytest.mark.parametrize(
    "mode", ["later-page", "unresolved", "count-mismatch", "duplicate-count"]
)
def test_github_cli_source_failures_keep_staging_unpublished(
    mode: str, tmp_path: Path
) -> None:
    write_failure_gh(tmp_path, mode)
    archive = tmp_path / "archive"
    result = run_github_cli(
        archive,
        tmp_path,
        to_text=(
            "2026-01-01T00:00:01+00:00"
            if mode == "unresolved"
            else "2026-01-02T00:00:00+00:00"
        ),
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        "incomplete" in result.stderr
        or "1,000" in result.stderr
        or "pagination" in result.stderr
    )
    assert not (archive / "runs").exists()
    assert len(list((archive / ".staging").iterdir())) == 1


def test_github_discovery_partitions_over_limit_results() -> None:
    class SearchFixture(_GitHub):
        def __init__(self) -> None:
            self.calls = 0

        def request(self, _endpoint: str, _accept: str = "") -> _Response:
            self.calls += 1
            total = 1001 if self.calls % 3 == 1 else 0
            return _Response(
                json.dumps(
                    {"total_count": total, "incomplete_results": False, "items": []}
                ).encode(),
                200,
                {},
                f"2026-01-01T00:00:0{self.calls}Z",
            )

    fixture = SearchFixture()
    reporter = build_test_reporter()
    reporter.emit(
        ProgressEvent(kind="start", task_id="github.discover", label="GitHub discovery")
    )
    candidates, coverage = _discover(
        fixture,
        "actor",
        CollectionRange.parse("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:02+00:00"),
        reporter,
    )
    reporter.emit(ProgressEvent(kind="finish", task_id="github.discover"))

    assert candidates == {}
    assert len(coverage) == 9
    assert sum(entry["disposition"] == "split" for entry in coverage) == 3
    assert all(
        entry["pagination_complete"]
        for entry in coverage
        if entry["disposition"] == "complete"
    )
    authorship_leaves = [
        entry
        for entry in coverage
        if entry["reason"] == "authorship" and entry["disposition"] == "complete"
    ]
    assert [entry["partition"] for entry in authorship_leaves] == [
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:01Z"},
        {"from": "2026-01-01T00:00:01Z", "to": "2026-01-01T00:00:02Z"},
    ]
    assert [entry["query"] for entry in authorship_leaves] == [
        "author:actor updated:2026-01-01T00:00:00Z..2026-01-01T00:00:00Z",
        "author:actor updated:2026-01-01T00:00:01Z..2026-01-01T00:00:01Z",
    ]


def test_github_discovery_rejects_incomplete_later_page() -> None:
    class IncompleteFixture(_GitHub):
        calls = 0

        def request(self, endpoint: str, _accept: str = "") -> _Response:
            self.calls += 1
            if self.calls == 1:
                return _Response(
                    b'{"total_count":1,"incomplete_results":false,"items":[]}',
                    200,
                    {"link": '<next>; rel="next"'},
                )
            return _Response(
                b'{"total_count":1,"incomplete_results":true,"items":[]}',
                200,
                {},
            )

    reporter = build_test_reporter()
    reporter.emit(
        ProgressEvent(kind="start", task_id="github.discover", label="GitHub discovery")
    )
    with pytest.raises(ArchiveError, match="incomplete"):
        _discover(
            IncompleteFixture(),
            "actor",
            CollectionRange.parse(
                "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"
            ),
            reporter,
        )
