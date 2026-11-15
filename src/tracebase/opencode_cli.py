"""OpenCode CLI execution with source-safe failure diagnostics."""

import subprocess

from .archive import ArchiveError
from .progress import ProgressEvent, ProgressReporter


class HydrationProgress:
    """The shared lifecycle for either OpenCode hydration protocol."""

    def __init__(self, reporter: ProgressReporter, total: int):
        self.reporter = reporter
        self.total = total
        reporter.emit(
            ProgressEvent(
                kind="start",
                task_id="opencode.hydrate",
                label="Hydrating OpenCode sessions",
                total=total,
            )
        )

    def update(self, completed: int, current: str, phase: str) -> None:
        self.reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="opencode.hydrate",
                completed=completed,
                total=self.total,
                current=current,
                phase=phase,
            )
        )

    def finish(self) -> None:
        self.reporter.emit(
            ProgressEvent(
                kind="finish",
                task_id="opencode.hydrate",
                completed=self.total,
                message=f"{self.total} sessions",
            )
        )


def run_opencode(arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["opencode", *arguments], capture_output=True, check=False
        )
    except OSError:
        raise ArchiveError("OpenCode CLI is unavailable") from None
    if result.returncode != 0:
        raise ArchiveError("OpenCode CLI operation failed")
    return result.stdout
