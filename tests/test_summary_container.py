import subprocess
from pathlib import Path

import pytest

from tracebase.summary_container import ContainerMounts, ContainerRunner, ensure_image

IMAGE = "tracebase-opencode:1.2.3"
VERSION = "1.2.3"


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

    ensure_image(IMAGE, tmp_path / "Dockerfile", VERSION)

    assert calls[0] == ["docker", "image", "inspect", IMAGE]
    assert calls[1] == [
        "docker",
        "build",
        "--tag",
        IMAGE,
        "--file",
        str(tmp_path / "Dockerfile"),
        "--build-arg",
        f"OPENCODE_VERSION={VERSION}",
        str(tmp_path),
    ]


def test_ensure_image_does_not_build_existing_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = record_docker_runs(monkeypatch, [0])

    ensure_image(IMAGE, tmp_path / "Dockerfile", VERSION)

    assert calls == [["docker", "image", "inspect", IMAGE]]


def test_dockerfile_requires_an_explicit_opencode_version() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "src/tracebase/container/Dockerfile"
    ).read_text(encoding="utf-8")

    assert 'ARG OPENCODE_VERSION\n\nRUN test -n "$OPENCODE_VERSION" \\\n' in dockerfile
    assert "ARG OPENCODE_VERSION=" not in dockerfile
    assert "https://astral.sh/uv/install.sh" in dockerfile
    assert "uv python install 3.14 --install-dir /opt/tracebase-python" in dockerfile
    assert "name python3.14 -print -quit" in dockerfile


def _container_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mutable_state: Path | None = None,
) -> list[str]:
    runner = ContainerRunner(
        "image",
        ContainerMounts(
            tmp_path / "context-evidence",
            tmp_path / "work",
            tmp_path / "results",
            tmp_path / "state",
            mutable_state=mutable_state,
        ),
    )
    calls: list[list[str]] = []

    def run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("tracebase.summary_container.subprocess.run", run)
    runner.run([], "{}", tmp_path / "stdout")
    return calls[0]


def test_container_mounts_only_model_visible_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_context = tmp_path / "context-evidence"
    command = _container_command(monkeypatch, tmp_path)

    assert f"{model_context}:/context:ro" in command


def test_container_mounts_host_mutable_state_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mutable_state = tmp_path / "MUTABLE_STATE.md"
    command = _container_command(monkeypatch, tmp_path, mutable_state=mutable_state)

    assert f"{mutable_state}:/work/MUTABLE_STATE.md:ro" in command
