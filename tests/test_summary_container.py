import subprocess
from pathlib import Path

import pytest

from tracebase.summary import IMAGE, OPENCODE_VERSION
from tracebase.summary_container import ensure_image


def record_docker_runs(
    monkeypatch: pytest.MonkeyPatch, returncodes: list[int]
) -> list[list[str]]:
    calls: list[list[str]] = []
    responses = iter(returncodes)

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, next(responses))

    monkeypatch.setattr("tracebase.summary_container.subprocess.run", run)
    return calls


def test_ensure_image_passes_authoritative_opencode_version_to_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = record_docker_runs(monkeypatch, [1, 0])

    ensure_image(IMAGE, tmp_path / "Dockerfile", OPENCODE_VERSION)

    assert calls[0] == ["docker", "image", "inspect", IMAGE]
    assert calls[1] == [
        "docker",
        "build",
        "--tag",
        IMAGE,
        "--file",
        str(tmp_path / "Dockerfile"),
        "--build-arg",
        f"OPENCODE_VERSION={OPENCODE_VERSION}",
        str(tmp_path),
    ]


def test_ensure_image_does_not_build_existing_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = record_docker_runs(monkeypatch, [0])

    ensure_image(IMAGE, tmp_path / "Dockerfile", OPENCODE_VERSION)

    assert calls == [["docker", "image", "inspect", IMAGE]]
