import base64
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from tracebase.archive import (
    Archive,
    CollectionRange,
    CollectionRun,
    Snapshot,
    decode_path_id,
    encode_path_id,
    uuid7,
)

PROJECT_ROOT = Path(__file__).parents[1]


class TestCollectionCli:
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tracebase", *arguments],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

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

    def test_unimplemented_opencode_collection_keeps_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "collect",
                "opencode",
                "--archive",
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
            assert "collector is not implemented" in result.stderr
            assert len(list((Path(directory) / ".staging").iterdir())) == 1
            assert not (Path(directory) / "runs").exists()

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
            run = CollectionRun(
                archive,
                "opencode",
                "instance-1",
                CollectionRange.parse(
                    "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"
                ),
                collector_version="test",
                effective_options={},
            )
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
        collection_range = CollectionRange.parse(
            "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"
        )
        run = CollectionRun(
            archive,
            "opencode",
            "instance-1",
            collection_range,
            collector_version="test",
            effective_options={"instance_id": "instance-1"},
        )
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

        next_run = CollectionRun(
            archive,
            "opencode",
            "instance-1",
            CollectionRange.parse(
                "2026-01-01T01:00:00+00:00", "2026-01-01T02:00:00+00:00"
            ),
            collector_version="test",
            effective_options={},
        )
        snapshot_root = next_run.write_snapshot(
            Snapshot(
                object_kind="opencode_session",
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

        snapshot_directory = (
            published / "snapshots/opencode_session/c2Vzc2lvbi9pZDp1bnNhZmU"
        )
        snapshot_manifest = json.loads(
            (snapshot_directory / "snapshot.json").read_text()
        )
        assert snapshot_manifest["source_id"] == "session/id:unsafe"
        assert (snapshot_directory / "session.json").read_bytes() == b"source-native"


def test_overlap_registry_uses_published_runs_and_half_open_ranges() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Archive(directory)
        published_range = CollectionRange.parse(
            "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"
        )
        run = CollectionRun(
            archive,
            "opencode",
            "instance-1",
            published_range,
            collector_version="test",
            effective_options={},
        )
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
