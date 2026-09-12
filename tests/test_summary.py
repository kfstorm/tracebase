import json
import subprocess
from pathlib import Path

import pytest

from tracebase import cli
from tracebase.context import ContextRequest
from tracebase.summary import (
    SummaryError,
    SummaryRequest,
    fingerprint_context,
    summarize,
    summarize_archive,
)
from tracebase.summary_shards import inspect_shards


class FakeRunner:
    def __init__(
        self,
        output: Path,
        *,
        recover: bool = False,
        incomplete: bool = False,
    ) -> None:
        self.output = output
        self.recover = recover
        self.incomplete = incomplete
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments: list[str],
        _config: str,
        _stdout_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if "--version" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "1.18.29\n", "")
        if "run" in arguments:
            if not self.recover or "--session" in arguments:
                work = self.output / "work"
                (work / "shards").mkdir(exist_ok=True)
                (work / "shards/repo.md").write_text("evidence", encoding="utf-8")
                status = "failed" if self.incomplete else "complete"
                (work / "NOTES.md").write_text(
                    "<!-- SHARD_STATUS_BEGIN -->\n"
                    + json.dumps(
                        {
                            "shards": [
                                {
                                    "id": "repo",
                                    "scope": "repo",
                                    "status": status,
                                    "retry_count": 0,
                                    "report": "/work/shards/repo.md",
                                }
                            ]
                        }
                    )
                    + "\n<!-- SHARD_STATUS_END -->\n",
                    encoding="utf-8",
                )
                (self.output / "results/summary.md").write_text(
                    "# Work summary\n", encoding="utf-8"
                )
            return subprocess.CompletedProcess(
                arguments, 0, '{"sessionID":"root"}\n', ""
            )
        raise AssertionError(f"unexpected runner call: {arguments}")


def context(tmp_path: Path) -> Path:
    result = tmp_path / "context"
    result.mkdir()
    (result / "index.md").write_text(
        "# Context Output\n\n"
        "Requested interval: `2026-01-01T01:00:00+01:00 <= t < "
        "2026-01-01T03:00:00+01:00`\n",
        encoding="utf-8",
    )
    return result


def bind_runner_to_staging(runner: FakeRunner, parent: Path) -> None:
    original_run = runner.run

    def run(arguments: list[str], config: str, stdout_path: Path | None = None):
        candidates = list(parent.glob(".summary-run.*"))
        if candidates:
            runner.output = candidates[0]
        return original_run(arguments, config, stdout_path)

    runner.run = run  # type: ignore[method-assign]


def test_existing_context_publishes_canonical_summary_and_provenance(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(output.parent / f".{output.name}.unused")

    bind_runner_to_staging(runner, tmp_path)
    published = summarize(
        SummaryRequest(source, "openai/model", "high", output), runner
    )

    assert published == output
    assert {path.name for path in output.iterdir()} == {"summary.md", "manifest.json"}
    assert (output / "summary.md").read_text() == "# Work summary\n"
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["model"] == "openai/model"
    assert manifest["variant"] == "high"
    assert manifest["opencode_version"] == "1.18.29"
    assert manifest["task_sha256"]
    assert manifest["context_input"] == fingerprint_context(source)
    assert manifest["requested_interval"] == {
        "from": "2026-01-01T01:00:00+01:00",
        "to": "2026-01-01T03:00:00+01:00",
    }
    assert "shards" not in manifest
    assert "metrics" not in manifest
    assert "canonical_result_recovery" not in manifest
    assert len(runner.calls) == 2


def test_missing_result_resumes_same_root_once(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(tmp_path, recover=True)
    bind_runner_to_staging(runner, tmp_path)
    debug = tmp_path / "debug"
    summarize(SummaryRequest(source, "model", None, output, debug_output=debug), runner)

    recovery_calls = [call for call in runner.calls if "--session" in call]
    assert len(recovery_calls) == 1
    assert recovery_calls[0][recovery_calls[0].index("--session") + 1] == "root"
    assert (debug / "runtime/root-recovery.json").is_file()
    assert len(runner.calls) == 3


def test_incomplete_shards_publish_nothing(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(tmp_path, incomplete=True)
    bind_runner_to_staging(runner, tmp_path)
    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert not output.exists()
    assert not list(tmp_path.glob(".summary.*"))


def test_debug_retains_existing_runtime_artifacts_without_extra_calls(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    debug = tmp_path / "debug"
    runner = FakeRunner(tmp_path)
    bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output, debug_output=debug), runner)

    assert (debug / "context/index.md").is_file()
    assert (debug / "work/TASK.md").is_file()
    assert (debug / "work/NOTES.md").is_file()
    assert (debug / "work/shards/repo.md").is_file()
    assert (debug / "runtime/stdout.jsonl").is_file()
    assert (debug / "runtime/stderr.log").is_file()
    assert not (debug / ".opencode-data").exists()
    assert not list(debug.rglob("auth.json"))
    assert len(runner.calls) == 2


def test_debug_excludes_credentials_written_to_work(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    debug = tmp_path / "debug"
    runner = FakeRunner(tmp_path)
    original_run = runner.run

    def run(arguments: list[str], config: str, stdout_path: Path | None = None):
        result = original_run(arguments, config, stdout_path)
        if "run" in arguments:
            (runner.output / "work/auth.json").write_text("secret", encoding="utf-8")
        return result

    runner.run = run  # type: ignore[method-assign]
    bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output, debug_output=debug), runner)

    assert not list(debug.rglob("auth.json"))


def test_failure_with_debug_retains_available_artifacts(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    debug = tmp_path / "debug"
    runner = FakeRunner(tmp_path, incomplete=True)
    bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(
            SummaryRequest(source, "model", None, output, debug_output=debug), runner
        )

    assert not output.exists()
    assert (debug / "runtime/stdout.jsonl").is_file()
    assert (debug / "work/shards/repo.md").is_file()
    assert (debug / "results/summary.md").read_text() == "# Work summary\n"
    assert json.loads((debug / "manifest.json").read_text())["status"] == "failed"
    assert len(runner.calls) == 2


def test_summary_rejects_output_inside_context(tmp_path: Path) -> None:
    source = context(tmp_path)

    with pytest.raises(SummaryError, match="must not overlap"):
        summarize(
            SummaryRequest(source, "model", None, source / "summary"),
            FakeRunner(tmp_path),
        )


def test_archive_mode_retains_explicit_context_when_summary_fails(
    tmp_path: Path, monkeypatch
) -> None:
    retained = tmp_path / "retained"

    def generate(_archive, _request, output):
        output.mkdir()
        (output / "index.md").write_text("valid Context", encoding="utf-8")
        return output

    monkeypatch.setattr("tracebase.summary.generate_context", generate)
    monkeypatch.setattr(
        "tracebase.summary.summarize",
        lambda _request: (_ for _ in ()).throw(SummaryError("failed")),
    )
    request = ContextRequest.parse(
        "2026-01-01T01:00:00+01:00", "2026-01-01T03:00:00+01:00"
    )

    with pytest.raises(SummaryError, match="failed"):
        summarize_archive(
            tmp_path / "archive",
            retained,
            request,
            "model",
            None,
            tmp_path / "summary",
        )

    assert (retained / "index.md").read_text() == "valid Context"


def test_archive_mode_rejects_outputs_overlapping_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    request = ContextRequest.parse(
        "2026-01-01T01:00:00+01:00", "2026-01-01T03:00:00+01:00"
    )

    with pytest.raises(SummaryError, match="Raw Archive"):
        summarize_archive(
            archive,
            archive / "context",
            request,
            "model",
            None,
            tmp_path / "summary",
        )


def test_shard_validation_rejects_retry_above_one(tmp_path: Path) -> None:
    (tmp_path / "NOTES.md").write_text(
        '<!-- SHARD_STATUS_BEGIN -->{"shards":[{"id":"a","status":"complete",'
        '"retry_count":2,"report":"/work/shards/a.md"}]}<!-- SHARD_STATUS_END -->',
        encoding="utf-8",
    )
    (tmp_path / "shards").mkdir()
    (tmp_path / "shards/a.md").write_text("report", encoding="utf-8")

    assert "retried more than once" in inspect_shards(tmp_path).errors[0]


def test_cli_rejects_conflicting_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    result = cli.main(
        [
            "summary",
            "--archive",
            "archive",
            "--context",
            "context",
            "--model",
            "model",
            "--output",
            "output",
        ]
    )

    assert result == 1
    assert "invalid command arguments" in capsys.readouterr().err


def test_cli_rejects_empty_archive_value(capsys: pytest.CaptureFixture[str]) -> None:
    result = cli.main(
        [
            "summary",
            "--archive",
            "",
            "--model",
            "model",
            "--output",
            "output",
        ]
    )

    assert result == 1
    assert "requires --from and --to" in capsys.readouterr().err


def test_cli_context_mode_dispatches_summary(tmp_path: Path, monkeypatch) -> None:
    source = context(tmp_path)
    output = tmp_path / "output"
    seen: list[SummaryRequest] = []
    monkeypatch.setattr(
        cli, "summarize", lambda request: seen.append(request) or output
    )

    result = cli.main(
        [
            "summary",
            "--context",
            str(source),
            "--model",
            "model",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert seen[0].context == source


def test_cli_archive_mode_composes_context_and_summary(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "output"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli, "summarize_archive", lambda *arguments: calls.append(arguments) or output
    )

    result = cli.main(
        [
            "summary",
            "--archive",
            "archive",
            "--from",
            "2026-01-01T01:00:00+01:00",
            "--to",
            "2026-01-01T03:00:00+01:00",
            "--model",
            "model",
            "--context-output",
            "retained-context",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert calls[0][0] == Path("archive")
    assert calls[0][1] == Path("retained-context")
