"""Progress events and the terminal presentation for Collection Runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A source-independent update emitted by a collector."""

    phase: str
    kind: str
    name: str = ""
    current: str = ""
    completed: int | None = None
    total: int | None = None
    detail: str = ""


ProgressCallback = Callable[[ProgressEvent], None]


class RichProgress:
    """Render collector events only when the destination is an interactive TTY."""

    def __init__(self, stream: TextIO) -> None:
        self._enabled = stream.isatty()
        self._tasks: dict[tuple[str, str], TaskID] = {}
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.fields[status]}"),
            console=Console(file=stream),
            redirect_stdout=False,
            redirect_stderr=False,
        )

    def __enter__(self) -> RichProgress:
        if self._enabled:
            self._progress.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._enabled:
            self._progress.stop()

    def emit(self, event: ProgressEvent) -> None:
        if not self._enabled:
            return
        if event.kind == "started":
            self._start_phase(event)
        elif event.kind == "page":
            self._update_discovery(event)
        elif event.kind == "candidate_found":
            self._update_phase(event, status=f"{event.completed} candidates")
        elif event.kind == "item_started":
            self._update_phase(event, status=event.current)
        elif event.kind == "item_completed":
            self._update_phase(event, completed=event.completed)
        elif event.kind == "completed":
            self._complete_phase(event)

    def _start_phase(self, event: ProgressEvent) -> None:
        key = (event.phase, event.name)
        if key in self._tasks:
            return
        description = event.detail or event.phase.capitalize()
        self._tasks[key] = self._progress.add_task(
            description,
            total=event.total,
            status="",
        )

    def _update_discovery(self, event: ProgressEvent) -> None:
        key = (event.phase, event.name)
        if key not in self._tasks:
            self._start_phase(
                ProgressEvent(
                    phase=event.phase,
                    kind="started",
                    name=event.name,
                    detail=f"  {event.name}",
                )
            )
        self._progress.update(
            self._tasks[key],
            completed=event.completed,
            status=event.detail,
        )

    def _update_phase(
        self,
        event: ProgressEvent,
        *,
        completed: int | None = None,
        status: str = "",
    ) -> None:
        key = (event.phase, "")
        task_id = self._tasks.get(key)
        if task_id is None:
            return
        if completed is not None:
            self._progress.update(task_id, completed=completed, status=status)
        else:
            self._progress.update(task_id, status=status)

    def _complete_phase(self, event: ProgressEvent) -> None:
        phase_tasks = [
            (task_id, self._progress.tasks[task_id])
            for (phase, _name), task_id in self._tasks.items()
            if phase == event.phase
        ]
        for task_id, task in phase_tasks:
            status = event.detail or "complete"
            if task.total is None:
                # Discovery has no stable denominator, so retain a static status only.
                self._progress.update(task_id, status=status)
                self._progress.console.print(
                    f"done {task.description} {status}", markup=False
                )
                self._progress.remove_task(task_id)
            else:
                self._progress.update(
                    task_id,
                    completed=task.total,
                    status=status,
                )
