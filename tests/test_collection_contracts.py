import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
FROM = "2026-01-01T00:00:00+00:00"
TO = "2026-01-01T01:00:00+00:00"
SOURCE_IDS = {
    "github": "artifact/source:1?秘密",
    "opencode": "session/source:1?秘密",
}
AUTHORIZATION_MARKER = "authorization-marker-18"
SOURCE_RESPONSE_MARKER = "source-response-marker-18"
SESSION_EXPORT_MARKER = "session-export-marker-18"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _make_github_fixture(directory: Path) -> None:
    source_id = repr(SOURCE_IDS["github"])
    _write_executable(
        directory / "gh",
        """#!/usr/bin/env python3
import json
import os
import sys

SOURCE_ID = __SOURCE_ID__
AUTHORIZATION_MARKER = "__AUTHORIZATION_MARKER__"
SOURCE_RESPONSE_MARKER = "__SOURCE_RESPONSE_MARKER__"

endpoint = sys.argv[-1]
if endpoint == "--help":
    print("gh api help")
    raise SystemExit(0)
if os.environ.get("TRACEBASE_FAIL_SOURCE") and endpoint.endswith("/issues/42"):
    print(AUTHORIZATION_MARKER, file=sys.stderr)
    print(SOURCE_RESPONSE_MARKER, file=sys.stderr)
    raise SystemExit(1)
if endpoint == "/user":
    body = {"node_id": "synthetic-actor", "login": "synthetic-actor"}
elif endpoint.startswith("/search/issues?"):
    body = {
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
elif endpoint == "/repos/example/project/issues/42":
    body = {"node_id": SOURCE_ID, "body": SOURCE_RESPONSE_MARKER}
elif endpoint == "/repos/example/project/issues/42/comments?per_page=100&page=1":
    body = [{"body": SOURCE_RESPONSE_MARKER + "-comment"}]
elif endpoint == "/repos/example/project/issues/42/timeline?per_page=100&page=1":
    body = [{"event": SOURCE_RESPONSE_MARKER + "-timeline"}]
else:
    raise SystemExit(2)

payload = json.dumps(body).encode()
sys.stdout.buffer.write(
    b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n" + payload
)
""".replace("__SOURCE_ID__", source_id)
        .replace("__AUTHORIZATION_MARKER__", AUTHORIZATION_MARKER)
        .replace("__SOURCE_RESPONSE_MARKER__", SOURCE_RESPONSE_MARKER),
    )


def _make_opencode_fixture(directory: Path) -> None:
    source_id = repr(SOURCE_IDS["opencode"])
    _write_executable(
        directory / "opencode",
        """#!/usr/bin/env python3
import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

SOURCE_ID = __SOURCE_ID__
AUTHORIZATION_MARKER = "__AUTHORIZATION_MARKER__"
SOURCE_RESPONSE_MARKER = "__SOURCE_RESPONSE_MARKER__"
SESSION_EXPORT_MARKER = "__SESSION_EXPORT_MARKER__"

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("synthetic-opencode-18")
elif arguments == [
    "serve", "--hostname", "127.0.0.1", "--port", "0", "--log-level", "ERROR"
]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            expected_query = {
                "start": ["1767225600000"],
                "archived": ["true"],
                "limit": ["10000"],
            }
            expected_auth = "Basic " + base64.b64encode(
                (
                    f"{os.environ['OPENCODE_SERVER_USERNAME']}:"
                    f"{os.environ['OPENCODE_SERVER_PASSWORD']}"
                ).encode()
            ).decode()
            if (
                parsed.path != "/experimental/session"
                or parse_qs(parsed.query) != expected_query
                or self.headers.get("Authorization") != expected_auth
            ):
                self.send_response(400)
                self.end_headers()
                return
            body = json.dumps([
                {
                    "id": SOURCE_ID,
                    "title": SOURCE_RESPONSE_MARKER,
                    "time": {"created": 1767225900000, "updated": 1767226200000},
                }
            ]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(
        f"opencode server listening on http://127.0.0.1:{server.server_port}",
        flush=True,
    )
    server.serve_forever()
elif arguments == ["export", SOURCE_ID]:
    if os.environ.get("TRACEBASE_FAIL_SOURCE"):
        print(AUTHORIZATION_MARKER, file=sys.stderr)
        print(SESSION_EXPORT_MARKER, file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.buffer.write(
        json.dumps({"export": SESSION_EXPORT_MARKER}).encode()
    )
else:
    raise SystemExit(2)
""".replace("__SOURCE_ID__", source_id)
        .replace("__AUTHORIZATION_MARKER__", AUTHORIZATION_MARKER)
        .replace("__SOURCE_RESPONSE_MARKER__", SOURCE_RESPONSE_MARKER)
        .replace("__SESSION_EXPORT_MARKER__", SESSION_EXPORT_MARKER),
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
    from_text: str = FROM,
    to_text: str = TO,
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
    return subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ
        | {
            "PATH": str(fixture_directory) + os.pathsep + os.environ["PATH"],
            "GH_TOKEN": AUTHORIZATION_MARKER,
            "TRACEBASE_FAIL_SOURCE": "1" if fail_source else "",
        },
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
    assert AUTHORIZATION_MARKER not in result.stdout
    assert SOURCE_RESPONSE_MARKER not in result.stdout
    assert SESSION_EXPORT_MARKER not in result.stdout

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
    assert run_manifest["collection_range"] == {"from": FROM, "to": TO}
    assert len(run_manifest["snapshots"]) == 1

    snapshot_entry = run_manifest["snapshots"][0]
    source_id = SOURCE_IDS[source]
    assert snapshot_entry["source_id"] == source_id
    snapshot_path = Path(snapshot_entry["path"])
    assert snapshot_path.parts[:2] == ("snapshots", snapshot_entry["object_kind"])
    assert _decode_path_id(snapshot_path.name) == source_id
    snapshot = published / snapshot_path
    snapshot_manifest = json.loads(
        (snapshot / "snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot_manifest["source_id"] == source_id

    if source == "github":
        assert snapshot_manifest["source_kind"] == "github"
        assert (snapshot / "issue.json").is_file()
        assert SOURCE_RESPONSE_MARKER.encode() in (snapshot / "issue.json").read_bytes()
    else:
        assert snapshot_manifest["source_kind"] == "opencode"
        assert (snapshot / "session.json").read_bytes() == json.dumps(
            {"export": SESSION_EXPORT_MARKER}
        ).encode()

    staging = archive / ".staging"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    archive_content = _archive_bytes(archive)
    assert AUTHORIZATION_MARKER.encode() not in archive_content
    assert SOURCE_RESPONSE_MARKER.encode() in archive_content
    if source == "opencode":
        assert SESSION_EXPORT_MARKER.encode() in archive_content
    else:
        assert SESSION_EXPORT_MARKER.encode() not in archive_content


@pytest.mark.parametrize(
    ("from_text", "to_text"),
    [
        ("2026-01-01T00:00:00", TO),
        ("2026-01-01T00:00:00.1Z", TO),
        (FROM, FROM),
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
    assert AUTHORIZATION_MARKER not in result.stderr
    assert SOURCE_RESPONSE_MARKER not in result.stderr
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
    assert AUTHORIZATION_MARKER not in second.stderr
    assert SOURCE_RESPONSE_MARKER not in second.stderr


def test_failed_sources_remain_inspectable_but_do_not_enter_overlap_registry(
    source_fixture: tuple[str, Path], tmp_path: Path
) -> None:
    source, fixture_directory = source_fixture
    archive = tmp_path / "archive"

    failed = _run_collect(source, archive, fixture_directory, fail_source=True)

    assert failed.returncode == 1
    assert failed.stdout == ""
    assert AUTHORIZATION_MARKER not in failed.stderr
    assert SOURCE_RESPONSE_MARKER not in failed.stderr
    assert SESSION_EXPORT_MARKER not in failed.stderr
    assert not (archive / "runs").exists()
    staging_runs = list((archive / ".staging").iterdir())
    assert len(staging_runs) == 1
    assert (staging_runs[0] / "snapshots").is_dir()
    assert AUTHORIZATION_MARKER.encode() not in _archive_bytes(archive)

    recovered = _run_collect(source, archive, fixture_directory)

    assert recovered.returncode == 0
    assert len(list((archive / "runs").iterdir())) == 1
