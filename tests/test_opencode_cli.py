import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tracebase.archive import encode_path_id

PROJECT_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "opencode"


class TestOpenCodeCollectionCli:
    def run_cli(
        self, archive: Path, fake_bin: Path, *arguments: str, fail_export: str = ""
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ | {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "TRACEBASE_SESSION_LIST": str(FIXTURES / "session-list.json"),
            "TRACEBASE_EXPORT_DIR": str(FIXTURES),
            "TRACEBASE_FAIL_EXPORT": fail_export,
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
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("1.18.29")
elif arguments == ["session", "list", "--format", "json"]:
    sys.stdout.buffer.write(Path(os.environ["TRACEBASE_SESSION_LIST"]).read_bytes())
elif len(arguments) == 2 and arguments[0] == "export":
    session_id = arguments[1]
    if session_id == os.environ["TRACEBASE_FAIL_EXPORT"]:
        print("sensitive-session-payload", file=sys.stderr)
        sys.exit(9)
    filenames = {
        "ends-at-start": "ends-at-start.json",
        "overlaps-start": "overlaps-start.json",
        "session/unsafe:1": "session-unsafe-1.json",
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

    def test_collects_intersecting_sessions_with_raw_exports_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(root / "archive", self.make_fake_opencode(root))

            assert result.returncode == 0
            assert result.stderr == ""
            assert result.stdout.count("\n") == 1
            published = next((root / "archive" / "runs").iterdir())
            run_manifest = json.loads((published / "run.json").read_text())
            assert run_manifest["source"] == {
                "kind": "opencode",
                "scope_id": "opaque-global-instance",
            }
            assert run_manifest["coverage"]["selected_session_count"] == 3
            assert {entry["source_id"] for entry in run_manifest["snapshots"]} == {
                "ends-at-start",
                "overlaps-start",
                "session/unsafe:1",
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
            assert metadata["session"]["directory"] == "/beta"

    def test_empty_range_publishes_manifest_only_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_cli(
                root / "archive",
                self.make_fake_opencode(root),
                "--from",
                "2026-01-01T02:00:00+00:00",
                "--to",
                "2026-01-01T03:00:00+00:00",
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
