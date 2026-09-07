import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from tracebase.archive import Archive, CollectionRange, CollectionRun, encode_path_id
from tracebase.opencode import collect
from tracebase.progress import ProgressEvent

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "opencode"


class RecordingReporter:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)


class TestOpenCodeCollectionCli:
    def run_cli(
        self,
        archive: Path,
        fake_bin: Path,
        *arguments: str,
        fail_export: str = "",
        next_cursor: str = "",
        discovery: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        from_text = "2026-01-01T00:00:00+00:00"
        to_text = "2026-01-01T01:00:00+00:00"
        if "--from" in arguments:
            from_text = arguments[arguments.index("--from") + 1]
        if "--to" in arguments:
            to_text = arguments[arguments.index("--to") + 1]
        environment = os.environ | {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "TRACEBASE_DISCOVERY": str(discovery or FIXTURES / "session-list.json"),
            "TRACEBASE_EXPORT_DIR": str(FIXTURES),
            "TRACEBASE_FAIL_EXPORT": fail_export,
            "TRACEBASE_NEXT_CURSOR": next_cursor,
            "TRACEBASE_EXPECTED_START": str(
                int(datetime.fromisoformat(from_text).timestamp() * 1000)
            ),
            "TRACEBASE_EXPECTED_CURSOR": str(
                int(datetime.fromisoformat(to_text).timestamp() * 1000)
            ),
            "TRACEBASE_SERVER_PID": str(fake_bin / "server.pid"),
        }
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tracebase",
                "collect",
                "opencode",
                "--archive",
                str(archive),
                "--instance-id",
                "opaque-global-instance",
                "--from",
                "2026-01-01T00:00:00+00:00",
                "--to",
                "2026-01-01T01:00:00+00:00",
                *arguments,
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def make_fake_opencode(self, directory: Path) -> Path:
        executable = directory / "opencode"
        executable.write_text(
            """#!/usr/bin/env python3
import base64
import json
import os
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("1.18.29")
elif arguments == [
    "serve", "--hostname", "127.0.0.1", "--port", "0", "--log-level", "ERROR"
]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            expected = {
                "start": [os.environ["TRACEBASE_EXPECTED_START"]],
                "cursor": [os.environ["TRACEBASE_EXPECTED_CURSOR"]],
                "archived": ["true"],
                "limit": ["10000"],
            }
            if (
                parsed.path != "/experimental/session"
                or parse_qs(parsed.query) != expected
                or self.headers.get("Authorization")
                != "Basic "
                + base64.b64encode(
                    (
                        f"{os.environ['OPENCODE_SERVER_USERNAME']}:"
                        f"{os.environ['OPENCODE_SERVER_PASSWORD']}"
                    ).encode()
                ).decode()
            ):
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            if os.environ["TRACEBASE_NEXT_CURSOR"]:
                cursor = "" if os.environ["TRACEBASE_NEXT_CURSOR"] == "empty" else "1"
                self.send_header("x-next-cursor", cursor)
            self.end_headers()
            self.wfile.write(Path(os.environ["TRACEBASE_DISCOVERY"]).read_bytes())

        def log_message(self, format, *args):
            pass

    if not os.environ.get("OPENCODE_SERVER_PASSWORD"):
        sys.exit(3)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    Path(os.environ["TRACEBASE_SERVER_PID"]).write_text(str(os.getpid()))
    print(
        f"opencode server listening on http://127.0.0.1:{server.server_port}",
        flush=True,
    )
    server.serve_forever()
elif len(arguments) == 2 and arguments[0] == "export":
    session_id = arguments[1]
    if session_id == os.environ["TRACEBASE_FAIL_EXPORT"]:
        print("sensitive-session-payload", file=sys.stderr)
        sys.exit(9)
    filenames = {
        "ends-at-start": "ends-at-start.json",
        "overlaps-start": "overlaps-start.json",
        "session/unsafe:1": "session-unsafe-1.json",
        "updated-after-end": "session-unsafe-1.json",
    }
    export_path = Path(os.environ["TRACEBASE_EXPORT_DIR"], filenames[session_id])
    sys.stdout.buffer.write(export_path.read_bytes())
else:
    sys.exit(2)
""",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return directory

    def test_collects_sessions_updated_in_range_with_raw_exports_and_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(root / "archive", self.make_fake_opencode(root))

            assert result.returncode == 0
            assert "START  Discovering OpenCode sessions" in result.stderr
            assert "DONE   Publishing archive: Archive published" in result.stderr
            assert result.stdout.count("\n") == 1
            published = next((root / "archive" / "runs").iterdir())
            run_manifest = json.loads((published / "run.json").read_text())
            assert run_manifest["source"] == {
                "kind": "opencode",
                "scope_id": "opaque-global-instance",
            }
            assert run_manifest["coverage"]["session_discovery_options"] == {
                "start": 1767225600000,
                "cursor": 1767229200000,
                "archived": True,
                "limit": 10000,
            }
            assert run_manifest["coverage"]["selected_session_count"] == 3
            assert {entry["source_id"] for entry in run_manifest["snapshots"]} == {
                "ends-at-start",
                "overlaps-start",
                "session/unsafe:1",
            }
            assert "updated-after-end" not in {
                entry["source_id"] for entry in run_manifest["snapshots"]
            }

            source_id = "session/unsafe:1"
            snapshot = published / "snapshots" / "session" / encode_path_id(source_id)
            assert (snapshot / "session.json").read_bytes() == (
                FIXTURES / "session-unsafe-1.json"
            ).read_bytes()
            metadata = json.loads((snapshot / "snapshot.json").read_text())["metadata"]
            assert metadata["export_command"] == ["opencode", "export", source_id]
            assert metadata["opencode_version"] == "1.18.29"
            assert metadata["effective_options"] == {
                "instance_id": "opaque-global-instance"
            }
            assert metadata["session"]["directory"] == "/gamma"
            assert metadata["session"]["parentID"] == "overlaps-start"
            assert metadata["session"]["time"]["archived"] == 1767228400000

    def test_collect_reports_discovery_hydration_and_publish_progress(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "tracebase.opencode._run_opencode",
            lambda arguments: b"1.18.29" if arguments == ["--version"] else b"export",
        )
        monkeypatch.setattr(
            "tracebase.opencode._start_server",
            lambda: (None, "", ""),
        )
        monkeypatch.setattr("tracebase.opencode._stop_server", lambda _server: None)
        monkeypatch.setattr(
            "tracebase.opencode._discover_sessions",
            lambda _url, _password, _run: [
                {
                    "id": "session-1",
                    "time": {"created": 1767225600000, "updated": 1767225600000},
                }
            ],
        )
        run = CollectionRun(
            Archive(tmp_path / "archive"),
            "opencode",
            "instance-1",
            CollectionRange.parse(
                "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"
            ),
            collector_version="test",
            effective_options={},
        )
        reporter = RecordingReporter()

        result = collect(run, reporter)

        assert run.snapshot_count == 1
        assert result.coverage["selected_session_count"] == 1
        assert run.staging.exists()
        assert not (tmp_path / "archive" / "runs").exists()
        assert [(event.task_id, event.kind) for event in reporter.events] == [
            ("opencode.discover", "start"),
            ("opencode.discover", "update"),
            ("opencode.discover", "finish"),
            ("opencode.hydrate", "start"),
            ("opencode.hydrate", "update"),
            ("opencode.hydrate", "update"),
            ("opencode.hydrate", "finish"),
        ]
        assert reporter.events[4].current == "session-1"
        assert reporter.events[4].total == 1

    def test_empty_range_publishes_manifest_only_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(
                root / "archive",
                self.make_fake_opencode(root),
                "--from",
                "2026-01-01T03:00:00+00:00",
                "--to",
                "2026-01-01T04:00:00+00:00",
            )

            assert result.returncode == 0
            published = next((root / "archive" / "runs").iterdir())
            assert json.loads((published / "run.json").read_text())["snapshots"] == []

    def test_export_failure_keeps_staging_and_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(
                root / "archive",
                self.make_fake_opencode(root),
                fail_export="session/unsafe:1",
            )

            assert result.returncode == 1
            assert result.stdout == ""
            assert "sensitive-session-payload" not in result.stderr
            assert len(list((root / "archive" / ".staging").iterdir())) == 1
            assert not (root / "archive" / "runs").exists()

    def test_invalid_boolean_session_timestamp_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            discovery = root / "invalid-sessions.json"
            discovery.write_text(
                '[{"id":"invalid","time":{"created":true,"updated":1767225600000}}]',
                encoding="utf-8",
            )
            result = self.run_cli(
                root / "archive",
                self.make_fake_opencode(root),
                discovery=discovery,
            )

            assert result.returncode == 1
            assert result.stdout == ""
            assert "session list is invalid" in result.stderr
            assert len(list((root / "archive" / ".staging").iterdir())) == 1
            assert not (root / "archive" / "runs").exists()

    def test_discovery_pagination_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(
                root / "archive",
                self.make_fake_opencode(root),
                next_cursor="1",
            )

            assert result.returncode == 1
            assert result.stdout == ""
            assert "discovery is incomplete" in result.stderr
            assert len(list((root / "archive" / ".staging").iterdir())) == 1
            assert not (root / "archive" / "runs").exists()
            server_pid = int((root / "server.pid").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(server_pid, 0)

    def test_empty_discovery_cursor_does_not_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(
                root / "archive", self.make_fake_opencode(root), next_cursor="empty"
            )

            assert result.returncode == 1
            assert "discovery is incomplete" in result.stderr
            assert not (root / "archive" / "runs").exists()
