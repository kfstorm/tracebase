"""Progress events and presentation reporters for Collection Runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)

ProgressKind = Literal["start", "update", "finish"]


class ProgressProtocolError(ValueError):
    """Raised when a reporter receives an invalid progress lifecycle event."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A source-independent update in a task's lifecycle."""

    kind: ProgressKind
    task_id: str
    label: str = ""
    parent_task_id: str | None = None
    completed: int | None = None
    total: int | None = None
    current: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"start", "update", "finish"}:
            raise ProgressProtocolError(f"unknown progress event kind: {self.kind!r}")


class ProgressReporter(Protocol):
    def emit(self, event: ProgressEvent) -> None: ...


class NullProgressReporter:
    """Discard progress events for callers that do not need presentation."""

    def emit(self, _event: ProgressEvent) -> None:
        pass


NULL_PROGRESS_REPORTER = NullProgressReporter()


ProgressClock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LineProgressReporter:
    """Write one operational progress line for every lifecycle event."""

    def __init__(self, stream: TextIO, clock: ProgressClock = _utc_now) -> None:
        self._stream = stream
        self._clock = clock
        self._tasks: dict[str, ProgressEvent] = {}

    def __enter__(self) -> LineProgressReporter:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def emit(self, event: ProgressEvent) -> None:
        if event.kind == "start":
            if event.task_id in self._tasks:
                raise ProgressProtocolError(f"task already started: {event.task_id}")
            self._tasks[event.task_id] = event
            self._write("START", event, self._start_status(event))
        elif event.kind == "update":
            task = self._tasks.get(event.task_id)
            if task is None:
                raise ProgressProtocolError(f"update for unknown task: {event.task_id}")
            self._tasks[event.task_id] = ProgressEvent(
                kind="start",
                task_id=task.task_id,
                label=task.label,
                parent_task_id=task.parent_task_id,
                completed=(
                    event.completed if event.completed is not None else task.completed
                ),
                total=event.total if event.total is not None else task.total,
                current=event.current,
                message=event.message,
            )
            self._write("UPDATE", event, self._update_status(task, event))
        elif event.kind == "finish":
            task = self._tasks.pop(event.task_id, None)
            if task is None:
                raise ProgressProtocolError(f"finish for unknown task: {event.task_id}")
            self._write("DONE", event, self._finish_status(task, event), task.label)

    @staticmethod
    def _start_status(event: ProgressEvent) -> str:
        return f"0/{event.total}" if event.total is not None else ""

    @staticmethod
    def _update_status(task: ProgressEvent, event: ProgressEvent) -> str:
        counts = ""
        total = event.total if event.total is not None else task.total
        completed = event.completed if event.completed is not None else task.completed
        if total is not None and completed is not None:
            counts = f"{completed}/{total}"
        return _join_status(counts, event.current, event.message)

    @staticmethod
    def _finish_status(task: ProgressEvent, event: ProgressEvent) -> str:
        counts = ""
        total = event.total if event.total is not None else task.total
        if total is not None and event.completed is not None:
            counts = f"{event.completed}/{total}"
        return _join_status(counts, event.current, event.message)

    def _write(
        self, kind: str, event: ProgressEvent, status: str, label: str = ""
    ) -> None:
        label = label or event.label or self._tasks.get(event.task_id, event).label
        label = label or event.task_id
        suffix = f": {status}" if status else ""
        timestamp = self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        self._stream.write(f"{timestamp} {kind:<6} {label}{suffix}\n")
        self._stream.flush()


def _join_status(*parts: str) -> str:
    return " ".join(part for part in parts if part)


class RichProgressReporter:
    """Render progress events as interactive Rich tasks."""

    def __init__(self, stream: TextIO) -> None:
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
        self._tasks: dict[str, TaskID] = {}
        self._totals: dict[str, int | None] = {}
        self._labels: dict[str, str] = {}

    def __enter__(self) -> RichProgressReporter:
        self._progress.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._progress.stop()

    def emit(self, event: ProgressEvent) -> None:
        if event.kind == "start":
            self._start(event)
        elif event.kind == "update":
            self._update(event)
        elif event.kind == "finish":
            self._finish(event)

    def _start(self, event: ProgressEvent) -> None:
        if event.task_id in self._tasks:
            raise ProgressProtocolError(f"task already started: {event.task_id}")
        self._tasks[event.task_id] = self._progress.add_task(
            event.label or event.task_id,
            total=event.total,
            status="",
        )
        self._totals[event.task_id] = event.total
        self._labels[event.task_id] = event.label or event.task_id

    def _update(self, event: ProgressEvent) -> None:
        task_id = self._tasks.get(event.task_id)
        if task_id is None:
            raise ProgressProtocolError(f"update for unknown task: {event.task_id}")
        updates: dict[str, Any] = {}
        if event.completed is not None:
            updates["completed"] = event.completed
        if event.total is not None:
            updates["total"] = event.total
            self._totals[event.task_id] = event.total
        if event.current or event.message:
            updates["status"] = _join_status(event.current, event.message)
        if updates:
            self._progress.update(task_id, **updates)

    def _finish(self, event: ProgressEvent) -> None:
        task_id = self._tasks.get(event.task_id)
        if task_id is None:
            raise ProgressProtocolError(f"finish for unknown task: {event.task_id}")
        self._tasks.pop(event.task_id)
        stored_total = self._totals.pop(event.task_id, None)
        total = event.total if event.total is not None else stored_total
        label = self._labels.pop(event.task_id, event.label or event.task_id)
        status = event.message or "complete"
        if total is None:
            self._progress.update(task_id, status=status)
            self._progress.console.print(f"done {label} {status}", markup=False)
            self._progress.remove_task(task_id)
            return
        updates: dict[str, Any] = {
            "completed": event.completed if event.completed is not None else total,
            "status": status,
        }
        if event.total is not None:
            updates["total"] = event.total
        self._progress.update(task_id, **updates)
