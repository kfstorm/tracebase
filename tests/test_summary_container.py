import json
import subprocess
from pathlib import Path

import pytest

from tracebase.summary_container import (
    ContainerError,
    ContainerMounts,
    ContainerRunner,
    ensure_image,
)

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


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (
            "",
            "  synthetic build error  \n",
            "could not build the Summarizer container image\nsynthetic build error",
        ),
        (
            "  synthetic build log  \n",
            "",
            "could not build the Summarizer container image\nsynthetic build log",
        ),
        (
            "  synthetic build log  \n",
            "  synthetic build error  \n",
            "could not build the Summarizer container image\n"
            "stdout:\nsynthetic build log\nstderr:\nsynthetic build error",
        ),
        (" \n", "\t", "could not build the Summarizer container image"),
    ],
)
def test_ensure_image_build_failure_includes_available_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    stderr: str,
    expected: str,
) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 1, stdout, stderr),
        ]
    )
    monkeypatch.setattr(
        "tracebase.summary_container.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )

    with pytest.raises(ContainerError) as error:
        ensure_image(IMAGE, tmp_path / "Dockerfile", VERSION)

    assert str(error.value) == expected


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
    arguments: list[str] | None = None,
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
    runner.run(arguments or [], "{}", tmp_path / "stdout")
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


def test_container_refreshes_model_catalog_before_running_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = _container_command(
        monkeypatch,
        tmp_path,
        arguments=["--pure", "run", "--model", "openai/example", "hi"],
    )

    assert command[command.index("-c") + 1] == (
        'opencode models --refresh >/dev/null && exec opencode "$@" > /export.stdout'
    )


@pytest.mark.parametrize(
    ("error_event", "expected"),
    [
        (
            {
                "type": "error",
                "error": {
                    "name": "UnknownError",
                    "data": {
                        "message": (
                            "Unexpected server error. Check server logs for details."
                        ),
                        "ref": "err_example123",
                    },
                },
            },
            "OpenCode error: UnknownError: Unexpected server error. "
            "Check server logs for details. (ref err_example123)",
        ),
        (
            {
                "type": "error",
                "error": {
                    "name": "ProviderError",
                    "data": {
                        "message": "Authorization: Bearer synthetic-secret",
                        "ref": "err_example456",
                    },
                },
            },
            "OpenCode error: ProviderError (ref err_example456)",
        ),
    ],
)
def test_failed_model_run_surfaces_safe_json_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_event: dict[str, object],
    expected: str,
) -> None:
    runner = ContainerRunner(
        "image",
        ContainerMounts(
            tmp_path / "context",
            tmp_path / "work",
            tmp_path / "results",
            tmp_path / "state",
        ),
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        capture = Path(
            command[command.index("--entrypoint") + 3].split(":/export.stdout:")[0]
        )
        capture.write_text(
            json.dumps({"type": "text", "part": {"text": "private evidence"}})
            + "\n"
            + json.dumps(error_event)
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("tracebase.summary_container.subprocess.run", run)

    with pytest.raises(ContainerError) as error:
        runner.run(["--pure", "run", "--format", "json"], "{}")

    assert str(error.value) == f"container command failed with exit code 1; {expected}"
    assert "private evidence" not in str(error.value)


def test_failed_container_without_json_error_reports_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = ContainerRunner(
        "image",
        ContainerMounts(
            tmp_path / "context",
            tmp_path / "work",
            tmp_path / "results",
            tmp_path / "state",
        ),
    )
    monkeypatch.setattr(
        "tracebase.summary_container.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 125, "", ""),
    )

    with pytest.raises(
        ContainerError, match="container command failed with exit code 125"
    ):
        runner.run(["--pure", "run", "--format", "json"], "{}")
