import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
COLLECTION_FROM = "2026-01-01T00:00:00+00:00"
COLLECTION_TO = "2026-01-01T01:00:00+00:00"
SOURCE_IDS = {
    "github": "artifact/source:1?秘密",
    "opencode": "session/source:1?秘密",
}
SECOND_SOURCE_IDS = {
    "github": "artifact/second:2?秘密",
    "opencode": "session/second:2?秘密",
}
AUTHORIZATION_MARKER = "authorization-marker-18"
SOURCE_RESPONSE_MARKER = "source-response-marker-18"
SESSION_EXPORT_MARKER = "session-export-marker-18"
GITHUB_ISSUE_RESPONSE = (
    '{"node_id": "artifact/source:1?秘密", "body": "source-response-marker-18"}'
).encode()
GITHUB_COMMENTS_RESPONSE = b'[{"body": "source-response-marker-18-comment"}]'
GITHUB_TIMELINE_RESPONSE = b'[{"event": "source-response-marker-18-timeline"}]'
OPENCODE_SESSION_EXPORT = b'{"export":"session-export-marker-18"}'


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _render_fixture(template: str, replacements: dict[str, str]) -> str:
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def _make_github_fixture(directory: Path) -> None:
    source_id = repr(SOURCE_IDS["github"])
    _write_executable(
        directory / "gh",
        _render_fixture(
            """#!/usr/bin/env python3
import json
import os
import sys

SOURCE_ID = __SOURCE_ID__
SECOND_SOURCE_ID = __SECOND_SOURCE_ID__
AUTHORIZATION_MARKER = "__AUTHORIZATION_MARKER__"
SOURCE_RESPONSE_MARKER = "__SOURCE_RESPONSE_MARKER__"
ISSUE_RESPONSE = __ISSUE_RESPONSE__
COMMENTS_RESPONSE = __COMMENTS_RESPONSE__
TIMELINE_RESPONSE = __TIMELINE_RESPONSE__

endpoint = sys.argv[-1]
if endpoint == "--help":
    print("gh api help")
    raise SystemExit(0)
if os.environ.get("TRACEBASE_FAIL_SOURCE") and endpoint.endswith("/issues/43"):
    print(AUTHORIZATION_MARKER, file=sys.stderr)
    print(SOURCE_RESPONSE_MARKER, file=sys.stderr)
    raise SystemExit(1)
if endpoint == "/user":
    payload = {"node_id": "synthetic-actor", "login": "synthetic-actor"}
elif endpoint.startswith("/search/issues?"):
    payload = {
        "total_count": 1,
        "incomplete_results": False,
        "items": [
            {
                "node_id": SOURCE_ID,
                "number": 42,
                "repository_url": "https://api.github.com/repos/example/project",
            }
        ],
    }
    if os.environ.get("TRACEBASE_FAIL_SOURCE"):
        payload["total_count"] = 2
        payload["items"].append(
            {
                "node_id": SECOND_SOURCE_ID,
                "number": 43,
                "repository_url": "https://api.github.com/repos/example/project",
            }
        )
elif endpoint == "/repos/example/project/issues/42":
    payload = ISSUE_RESPONSE
elif endpoint == "/repos/example/project/issues/42/comments?per_page=100&page=1":
    payload = COMMENTS_RESPONSE
elif endpoint == "/repos/example/project/issues/42/timeline?per_page=100&page=1":
    payload = TIMELINE_RESPONSE
else:
    raise SystemExit(2)

if not isinstance(payload, bytes):
    payload = json.dumps(payload).encode()
sys.stdout.buffer.write(
    b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n" + payload
)
""",
            {
                "__SOURCE_ID__": source_id,
                "__SECOND_SOURCE_ID__": repr(SECOND_SOURCE_IDS["github"]),
                "__AUTHORIZATION_MARKER__": AUTHORIZATION_MARKER,
                "__SOURCE_RESPONSE_MARKER__": SOURCE_RESPONSE_MARKER,
                "__ISSUE_RESPONSE__": repr(GITHUB_ISSUE_RESPONSE),
                "__COMMENTS_RESPONSE__": repr(GITHUB_COMMENTS_RESPONSE),
                "__TIMELINE_RESPONSE__": repr(GITHUB_TIMELINE_RESPONSE),
            },
        ),
    )


def _make_opencode_fixture(directory: Path) -> None:
    source_id = repr(SOURCE_IDS["opencode"])
    second_source_id = repr(SECOND_SOURCE_IDS["opencode"])
    _write_executable(
        directory / "opencode",
        _render_fixture(
            """#!/usr/bin/env python3
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SOURCE_ID = __SOURCE_ID__
SECOND_SOURCE_ID = __SECOND_SOURCE_ID__
AUTHORIZATION_MARKER = "__AUTHORIZATION_MARKER__"
SOURCE_RESPONSE_MARKER = "__SOURCE_RESPONSE_MARKER__"
SESSION_EXPORT_MARKER = "__SESSION_EXPORT_MARKER__"
SESSION_EXPORT = __SESSION_EXPORT__

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("synthetic-opencode-18")
elif arguments == [
    "serve", "--hostname", "127.0.0.1", "--port", "0", "--log-level", "ERROR"
]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps([
                {
                    "id": SOURCE_ID,
                    "title": SOURCE_RESPONSE_MARKER,
                    "time": {"created": 1767225900000, "updated": 1767226200000},
                }
            ] + ([
                {
                    "id": SECOND_SOURCE_ID,
                    "title": SOURCE_RESPONSE_MARKER + "-second",
                    "time": {"created": 1767225900000, "updated": 1767226200000},
                }
            ] if os.environ.get("TRACEBASE_FAIL_SOURCE") else [])).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Path(os.environ["TRACEBASE_PASSWORD_CAPTURE"]).write_text(
        os.environ["OPENCODE_SERVER_PASSWORD"], encoding="utf-8"
    )
    print(
        f"opencode server listening on http://127.0.0.1:{server.server_port}",
        flush=True,
    )
    server.serve_forever()
elif arguments == ["export", SOURCE_ID]:
    sys.stdout.buffer.write(SESSION_EXPORT)
elif arguments == ["export", SECOND_SOURCE_ID]:
    if os.environ.get("TRACEBASE_FAIL_SOURCE"):
        password = Path(os.environ["TRACEBASE_PASSWORD_CAPTURE"]).read_text(
            encoding="utf-8"
        )
        print(password, file=sys.stderr)
        print(AUTHORIZATION_MARKER, file=sys.stderr)
        print(SESSION_EXPORT_MARKER, file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.buffer.write(SESSION_EXPORT)
else:
    raise SystemExit(2)
""",
            {
                "__SOURCE_ID__": source_id,
                "__SECOND_SOURCE_ID__": second_source_id,
                "__AUTHORIZATION_MARKER__": AUTHORIZATION_MARKER,
                "__SOURCE_RESPONSE_MARKER__": SOURCE_RESPONSE_MARKER,
                "__SESSION_EXPORT_MARKER__": SESSION_EXPORT_MARKER,
                "__SESSION_EXPORT__": repr(OPENCODE_SESSION_EXPORT),
            },
        ),
    )


def _make_source_fixture(directory: Path, source: str) -> None:
    directory.mkdir()
    if source == "github":
        _make_github_fixture(directory)
    else:
        _make_opencode_fixture(directory)


def _run_collect(
    source: str,
    archive: Path,
    fixture_directory: Path,
    *,
    from_text: str = COLLECTION_FROM,
    to_text: str = COLLECTION_TO,
    fail_source: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        "-m",
        "tracebase",
        "collect",
        source,
        "--archive",
        str(archive),
        "--from",
        from_text,
        "--to",
        to_text,
    ]
    if source == "opencode":
        arguments.extend(["--instance-id", "synthetic-instance-18"])
    environment = os.environ | {
        "PATH": str(fixture_directory) + os.pathsep + os.environ["PATH"],
        "GH_TOKEN": AUTHORIZATION_MARKER,
        "TRACEBASE_FAIL_SOURCE": "1" if fail_source else "",
        "TRACEBASE_PASSWORD_CAPTURE": str(archive.parent / "opencode-password.capture"),
    }
    if source == "opencode":
        environment["PYTHONPATH"] = (
            str(fixture_directory) + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
    return subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def _archive_bytes(root: Path) -> bytes:
    return b"".join(
        path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()
    )


def _decode_path_id(path_id: str) -> str:
    assert path_id
    assert "=" not in path_id
    padding = "=" * (-len(path_id) % 4)
    return base64.urlsafe_b64decode(path_id + padding).decode("utf-8")


def _encode_path_id(source_id: str) -> str:
    return base64.urlsafe_b64encode(source_id.encode("utf-8")).decode().rstrip("=")


def _assert_authorization_is_not_disclosed(
    result: subprocess.CompletedProcess[str], archive: Path
) -> None:
    sensitive_values = [AUTHORIZATION_MARKER]
    password_capture = archive.parent / "opencode-password.capture"
    if password_capture.exists():
        password = password_capture.read_text(encoding="utf-8")
        assert password
        sensitive_values.append(password)
    output = result.stdout + result.stderr
    archive_content = _archive_bytes(archive)
    for value in sensitive_values:
        assert value not in output
        assert value.encode() not in archive_content


@pytest.fixture(params=["github", "opencode"])
def source_fixture(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[str, Path]:
    source = request.param
    fixture_directory = tmp_path / "bin"
    _make_source_fixture(fixture_directory, source)
    return source, fixture_directory


def test_successful_sources_share_the_published_archive_contract(
    source_fixture: tuple[str, Path], tmp_path: Path
) -> None:
    source, fixture_directory = source_fixture
    archive = tmp_path / "archive"

    result = _run_collect(source, archive, fixture_directory)

    assert result.returncode == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert SOURCE_RESPONSE_MARKER not in result.stdout
    assert SESSION_EXPORT_MARKER not in result.stdout
    _assert_authorization_is_not_disclosed(result, archive)

    published_runs = list((archive / "runs").iterdir())
    assert len(published_runs) == 1
    published = published_runs[0]
    assert published.name in result.stdout
    assert "1 snapshots" in result.stdout
    assert {path.name for path in published.iterdir()} == {"run.json", "snapshots"}

    run_manifest = json.loads((published / "run.json").read_text(encoding="utf-8"))
    expected_scope = (
        {"kind": "github", "scope_id": "synthetic-actor"}
        if source == "github"
        else {"kind": "opencode", "scope_id": "synthetic-instance-18"}
    )
    assert run_manifest["source"] == expected_scope
    assert run_manifest["collection_range"] == {
        "from": COLLECTION_FROM,
        "to": COLLECTION_TO,
    }
    assert len(run_manifest["snapshots"]) == 1

    snapshot_entry = run_manifest["snapshots"][0]
    source_id = SOURCE_IDS[source]
    assert snapshot_entry["source_id"] == source_id
    snapshot_path = Path(snapshot_entry["path"])
    assert snapshot_path.parts[:2] == ("snapshots", snapshot_entry["object_kind"])
    assert snapshot_path.name == _encode_path_id(source_id)
    assert _decode_path_id(snapshot_path.name) == source_id
    snapshot = published / snapshot_path
    snapshot_manifest = json.loads(
        (snapshot / "snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot_manifest["source_id"] == source_id

    if source == "github":
        assert snapshot_manifest["source_kind"] == "github"
        assert (snapshot / "issue.json").read_bytes() == GITHUB_ISSUE_RESPONSE
        assert (snapshot / "comments.001.json").read_bytes() == GITHUB_COMMENTS_RESPONSE
        assert (snapshot / "timeline.001.json").read_bytes() == GITHUB_TIMELINE_RESPONSE
    else:
        assert snapshot_manifest["source_kind"] == "opencode"
        assert (snapshot / "session.json").read_bytes() == OPENCODE_SESSION_EXPORT

    staging = archive / ".staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    archive_content = _archive_bytes(archive)
    assert SOURCE_RESPONSE_MARKER.encode() in archive_content
    if source == "opencode":
        assert SESSION_EXPORT_MARKER.encode() in archive_content
    else:
        assert SESSION_EXPORT_MARKER.encode() not in archive_content


@pytest.mark.parametrize(
    ("from_text", "to_text"),
    [
        ("2026-01-01T00:00:00", COLLECTION_TO),
        ("2026-01-01T00:00:00.1Z", COLLECTION_TO),
        ("not-a-timestamp", COLLECTION_TO),
        (COLLECTION_FROM, COLLECTION_FROM),
        (COLLECTION_TO, COLLECTION_FROM),
    ],
)
def test_both_commands_reject_invalid_ranges_before_staging(
    source_fixture: tuple[str, Path],
    tmp_path: Path,
    from_text: str,
    to_text: str,
) -> None:
    source, fixture_directory = source_fixture
    archive = tmp_path / "archive"

    result = _run_collect(
        source,
        archive,
        fixture_directory,
        from_text=from_text,
        to_text=to_text,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert SOURCE_RESPONSE_MARKER not in result.stderr
    _assert_authorization_is_not_disclosed(result, archive)
    assert not archive.exists()


def test_both_commands_reject_matching_published_overlap(
    source_fixture: tuple[str, Path], tmp_path: Path
) -> None:
    source, fixture_directory = source_fixture
    archive = tmp_path / "archive"

    first = _run_collect(source, archive, fixture_directory)
    assert first.returncode == 0
    published_runs = list((archive / "runs").iterdir())

    second = _run_collect(source, archive, fixture_directory)

    assert second.returncode == 1
    assert second.stdout == ""
    assert "overlaps" in second.stderr
    assert list((archive / "runs").iterdir()) == published_runs
    assert list((archive / ".staging").iterdir()) == []
    assert SOURCE_RESPONSE_MARKER not in second.stderr
    _assert_authorization_is_not_disclosed(second, archive)


def test_failed_sources_remain_inspectable_but_do_not_enter_overlap_registry(
    source_fixture: tuple[str, Path], tmp_path: Path
) -> None:
    source, fixture_directory = source_fixture
    archive = tmp_path / "archive"

    failed = _run_collect(source, archive, fixture_directory, fail_source=True)

    assert failed.returncode == 1
    assert failed.stdout == ""
    assert SOURCE_RESPONSE_MARKER not in failed.stderr
    assert SESSION_EXPORT_MARKER not in failed.stderr
    assert not (archive / "runs").exists()
    staging_runs = list((archive / ".staging").iterdir())
    assert len(staging_runs) == 1
    staging_snapshot = (
        staging_runs[0]
        / "snapshots"
        / ("issue" if source == "github" else "session")
        / _encode_path_id(SOURCE_IDS[source])
    )
    assert staging_snapshot.is_dir()
    if source == "github":
        assert (staging_snapshot / "issue.json").read_bytes() == GITHUB_ISSUE_RESPONSE
    else:
        assert (
            staging_snapshot / "session.json"
        ).read_bytes() == OPENCODE_SESSION_EXPORT
    _assert_authorization_is_not_disclosed(failed, archive)

    recovered = _run_collect(source, archive, fixture_directory)

    assert recovered.returncode == 0
    assert len(list((archive / "runs").iterdir())) == 1
