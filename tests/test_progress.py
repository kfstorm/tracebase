from datetime import UTC, datetime
from io import StringIO

import pytest

from tracebase.progress import (
    LineProgressSink,
    ProgressEvent,
    ProgressProtocolError,
    ProgressReporter,
    RichProgressSink,
)


class TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_rich_progress_renders_complete_lifecycle_after_removed_task() -> None:
    stream = TTYBuffer()

    with RichProgressSink(stream) as progress:
        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="github.discover",
                label="Discovering GitHub artifacts",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="update",
                task_id="github.discover",
                completed=1,
                message="Authorship: page 1, 2 candidates",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="finish",
                task_id="github.discover",
                message="2 candidates",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="github.hydrate",
                total=2,
                label="Hydrating GitHub artifacts",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="update",
                task_id="github.hydrate",
                completed=1,
                total=2,
                phase="completed",
                current="octo/example#7",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="finish",
                task_id="github.hydrate",
                completed=2,
                message="2 artifacts",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="publish",
                label="Publishing archive",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="finish",
                task_id="publish",
                message="Archive published",
            )
        )

        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="empty",
                label="Empty phase",
                total=0,
            )
        )
        progress.emit(
            ProgressEvent(
                kind="finish",
                task_id="empty",
                completed=0,
            )
        )

    output = stream.getvalue()
    assert "Discovering GitHub artifacts" in output
    assert "Hydrating GitHub artifacts" in output
    assert "Publishing archive" in output
    assert "Empty phase" in output


def test_line_progress_reports_complete_lifecycle_for_non_tty() -> None:
    stream = StringIO()
    timestamp = datetime(2026, 9, 7, 4, 1, 22, tzinfo=UTC)

    with LineProgressSink(stream, clock=lambda: timestamp) as progress:
        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="github.discover",
                label="Discovering GitHub artifacts",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="update",
                task_id="github.discover",
                completed=1,
                message="Authorship: page 1, 37 candidates",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="finish",
                task_id="github.discover",
                message="81 candidates",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="github.hydrate",
                label="Hydrating GitHub artifacts",
                total=81,
            )
        )
        progress.emit(
            ProgressEvent(
                kind="update",
                task_id="github.hydrate",
                completed=1,
                phase="completed",
                current="kfstorm/foo#42",
            )
        )
        progress.emit(
            ProgressEvent(
                kind="finish",
                task_id="github.hydrate",
                completed=81,
                message="81 artifacts",
            )
        )

    assert stream.getvalue().splitlines() == [
        "2026-09-07T04:01:22Z START  Discovering GitHub artifacts",
        "2026-09-07T04:01:22Z UPDATE Discovering GitHub artifacts: "
        "Authorship: page 1, 37 candidates",
        "2026-09-07T04:01:22Z DONE   Discovering GitHub artifacts: 81 candidates",
        "2026-09-07T04:01:22Z START  Hydrating GitHub artifacts: 0/81",
        "2026-09-07T04:01:22Z UPDATE Hydrating GitHub artifacts: "
        "1/81 completed kfstorm/foo#42",
        "2026-09-07T04:01:22Z DONE   Hydrating GitHub artifacts: 81/81 81 artifacts",
    ]


@pytest.mark.parametrize("sink_type", [LineProgressSink, RichProgressSink])
def test_progress_reporter_fails_fast_on_invalid_task_lifecycle(
    sink_type: type,
) -> None:
    stream = TTYBuffer() if sink_type is RichProgressSink else StringIO()
    sink = sink_type(stream)
    reporter = ProgressReporter(sink)

    with sink:
        reporter.emit(ProgressEvent(kind="start", task_id="task", label="Task"))
        with pytest.raises(ProgressProtocolError, match="already started"):
            reporter.emit(ProgressEvent(kind="start", task_id="task", label="Task"))
        with pytest.raises(ProgressProtocolError, match="unknown task"):
            reporter.emit(ProgressEvent(kind="update", task_id="missing"))
        with pytest.raises(ProgressProtocolError, match="unknown task"):
            reporter.emit(ProgressEvent(kind="finish", task_id="missing"))


def test_progress_event_rejects_unknown_kind() -> None:
    with pytest.raises(ProgressProtocolError, match="unknown progress event kind"):
        ProgressEvent(kind="udpate", task_id="task")  # type: ignore[arg-type]


def test_progress_reporter_state_is_not_shared_between_runs() -> None:
    first = ProgressReporter(LineProgressSink(StringIO()))
    second = ProgressReporter(LineProgressSink(StringIO()))

    first.emit(ProgressEvent(kind="start", task_id="task"))
    second.emit(ProgressEvent(kind="start", task_id="task"))
