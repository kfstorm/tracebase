"""Docker execution with intentionally narrow Summarizer mounts."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ContainerError(RuntimeError):
    """Raised when the evaluation container cannot run a command."""


@dataclass(frozen=True, slots=True)
class ContainerMounts:
    context: Path
    work: Path
    results: Path
    opencode_data: Path
    summary: Path | None = None
    task: Path | None = None


class ContainerRunner:
    def __init__(self, image: str, mounts: ContainerMounts):
        self.image = image
        self.mounts = mounts

    def _command(
        self, arguments: list[str], config: str, stdout_path: Path
    ) -> list[str]:
        mounts = self.mounts
        command = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,size=256m",
            "--tmpfs",
            f"/home/eval:rw,nosuid,uid={os.getuid()},gid={os.getgid()},mode=755,size=256m",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            "/work",
            "--volume",
            f"{mounts.context}:/context:ro",
            "--volume",
            f"{mounts.work}:/work:rw",
            "--volume",
            f"{mounts.results}:/results:rw",
            "--volume",
            f"{mounts.opencode_data}:/home/eval/.local:rw",
        ]
        if mounts.summary is not None:
            command.extend(["--volume", f"{mounts.summary}:/summary.md:ro"])
        if mounts.task is not None:
            command.extend(["--volume", f"{mounts.task}:/work/TASK.md:ro"])
        command.extend(
            [
                "--entrypoint",
                "/bin/sh",
                "--volume",
                f"{stdout_path}:/export.stdout:rw",
            ]
        )
        command.extend(
            [
                "--env",
                "HOME=/home/eval",
                "--env",
                "XDG_DATA_HOME=/home/eval/.local/share",
                "--env",
                "NO_COLOR=1",
                "--env",
                f"OPENCODE_CONFIG_CONTENT={config}",
                self.image,
            ]
        )
        command.extend(["-c", 'exec opencode "$@" > /export.stdout', "opencode"])
        command.extend(arguments)
        return command

    def run(
        self, arguments: list[str], config: str, stdout_path: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                capture_path = stdout_path or Path(temporary_directory) / "stdout"
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                capture_path.write_text("", encoding="utf-8")
                result = subprocess.run(
                    self._command(arguments, config, capture_path),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                captured_stdout = capture_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ContainerError("could not prepare container output") from error
        if result.returncode != 0:
            raise ContainerError(
                f"container command failed with exit code {result.returncode}"
            )
        return subprocess.CompletedProcess(
            result.args, result.returncode, captured_stdout, result.stderr
        )


def ensure_image(image: str, dockerfile: Path) -> None:
    """Build the pinned local image once when it is not already available."""
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ContainerError("Docker is unavailable") from error
    if inspected.returncode == 0:
        return
    result = subprocess.run(
        [
            "docker",
            "build",
            "--tag",
            image,
            "--file",
            str(dockerfile),
            str(dockerfile.parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ContainerError("could not build the Summarizer container image")
