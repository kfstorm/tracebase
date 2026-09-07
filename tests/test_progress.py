from io import StringIO

from tracebase.progress import ProgressEvent, RichProgress


class TTYBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_rich_progress_renders_discovery_and_hydration_tasks() -> None:
    stream = TTYBuffer()

    with RichProgress(stream) as progress:
        progress.emit(
            ProgressEvent(
                phase="discover",
                kind="started",
                detail="Discovering GitHub artifacts",
            )
        )
        progress.emit(
            ProgressEvent(
                phase="discover",
                kind="page",
                name="authorship",
                completed=1,
                detail="1 page - 2 candidates",
            )
        )
        progress.emit(
            ProgressEvent(
                phase="discover",
                kind="completed",
                detail="2 candidates",
            )
        )
        progress.emit(
            ProgressEvent(
                phase="hydrate",
                kind="started",
                total=2,
                detail="Hydrating GitHub artifacts",
            )
        )
        progress.emit(
            ProgressEvent(
                phase="hydrate",
                kind="item_started",
                current="octo/example#7",
            )
        )

    output = stream.getvalue()
    assert "Discovering GitHub artifacts" in output
    assert "authorship" in output
    assert "Hydrating GitHub artifacts" in output
    assert "octo/example#7" in output
    assert "100%" not in output


def test_rich_progress_is_silent_for_non_tty() -> None:
    stream = StringIO()

    with RichProgress(stream) as progress:
        progress.emit(
            ProgressEvent(phase="publish", kind="started", detail="Publishing archive")
        )

    assert stream.getvalue() == ""
