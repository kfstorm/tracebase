import json
import subprocess
from pathlib import Path

import pytest

from tracebase import cli
from tracebase.context import ContextRequest
from tracebase.summary import (
    IMAGE,
    OPENCODE_VERSION,
    SummaryError,
    SummaryRequest,
    fingerprint_context,
    summarize,
    summarize_archive,
)
from tracebase.summary_shards import inspect_shard_plan, inspect_shards


class FakeRunner:
    def __init__(
        self,
        output: Path,
        *,
        recover: bool = False,
        incomplete: bool = False,
        missing_context_section: bool = False,
    ) -> None:
        self.output = output
        self.recover = recover
        self.incomplete = incomplete
        self.missing_context_section = missing_context_section
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments: list[str],
        _config: str,
        _stdout_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if "--version" in arguments:
            return subprocess.CompletedProcess(
                arguments, 0, OPENCODE_VERSION + "\n", ""
            )
        if "run" in arguments:
            if not self.recover or "--session" in arguments:
                work = self.output / "work"
                (work / "shards").mkdir(exist_ok=True)
                (work / "shards/repo.md").write_text(
                    "## User work\n\nevidence\n"
                    + (
                        ""
                        if self.missing_context_section
                        else "\n## Context-only evidence\n\nNone.\n"
                    ),
                    encoding="utf-8",
                )
                status = "failed" if self.incomplete else "complete"
                (work / "NOTES.md").write_text(
                    "<!-- SHARD_STATUS_BEGIN -->\n"
                    + json.dumps(
                        {
                            "shards": [
                                {
                                    "id": "repo",
                                    "scope": "/context/repo",
                                    "attribution_modes": ["personal"],
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
        "2026-01-01T03:00:00+01:00`\n"
        "- **repo** [attribution mode: `personal`](repo/overview.md)\n",
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
    assert manifest["opencode_version"] == OPENCODE_VERSION
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


def test_summarize_uses_same_version_for_image_tag_and_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(tmp_path)
    bind_runner_to_staging(runner, tmp_path)
    builds: list[tuple[str, Path, str]] = []

    monkeypatch.setattr("tracebase.summary.prepare_state", lambda *_args: "{}")
    monkeypatch.setattr(
        "tracebase.summary.ensure_image",
        lambda image, dockerfile, opencode_version: builds.append(
            (image, dockerfile, opencode_version)
        ),
    )
    monkeypatch.setattr(
        "tracebase.summary.ContainerRunner", lambda _image, _mounts: runner
    )

    summarize(SummaryRequest(source, "model", None, output))

    assert f"tracebase-opencode:{OPENCODE_VERSION}" == IMAGE
    assert builds == [
        (
            IMAGE,
            Path(__file__).parents[1] / "src/tracebase/container/Dockerfile",
            OPENCODE_VERSION,
        )
    ]


def test_summarizer_contract_requires_attribution_before_sharded_synthesis() -> None:
    prompt = (
        Path(__file__).parents[1] / "src/tracebase/prompts/summarizer-v1.md"
    ).read_text(encoding="utf-8")

    assert "final Summary is a projection of the user's work" in prompt
    assert "`personal`" in prompt and "`actor_scoped`" in prompt
    assert "authoritative attribution" in prompt
    assert "same review thread" in prompt
    assert "single `## Commits` section" in prompt
    assert "## User work" in prompt
    assert "## Context-only evidence" in prompt
    assert "Only `User work` may be promoted" in prompt
    assert "Repository ownership" in prompt


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


def test_missing_shard_section_fails_without_mutating_worker_report(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    debug = tmp_path / "debug"
    runner = FakeRunner(tmp_path, missing_context_section=True)
    bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(
            SummaryRequest(source, "model", None, output, debug_output=debug), runner
        )

    assert not output.exists()
    report = debug / "work/shards/repo.md"
    assert report.read_text(encoding="utf-8") == "## User work\n\nevidence\n"


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
    assert "## Context-only evidence" in (debug / "work/shards/repo.md").read_text()
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
        '"attribution_modes":["personal"],"retry_count":2,'
        '"report":"/work/shards/a.md"}]}<!-- SHARD_STATUS_END -->',
        encoding="utf-8",
    )
    (tmp_path / "shards").mkdir()
    (tmp_path / "shards/a.md").write_text("report", encoding="utf-8")

    assert "retried more than once" in inspect_shards(tmp_path).errors[0]


def test_shard_validation_rejects_context_attribution_mismatch(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **repo** [attribution mode: `personal`](repo/overview.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "NOTES.md").write_text(
        '<!-- SHARD_STATUS_BEGIN -->{"shards":[{"id":"repo",'
        '"scope":"/context/repo","attribution_modes":["actor_scoped"],'
        '"status":"complete","retry_count":0,"report":"/work/shards/repo.md"}]}'
        "<!-- SHARD_STATUS_END -->",
        encoding="utf-8",
    )
    (tmp_path / "shards").mkdir()
    (tmp_path / "shards/repo.md").write_text(
        "## User work\n\nnone\n\n## Context-only evidence\n\nnone\n",
        encoding="utf-8",
    )

    assert "do not match Context" in inspect_shards(tmp_path, context_dir).errors[0]


def test_shard_plan_accepts_a_complete_partition(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/2/overview.md)\n",
        encoding="utf-8",
    )
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/repo/issue/{1}",
                "attribution_modes": ["personal"],
            },
            {
                "id": "two",
                "scope": "github/acme/repo/issue/{2}",
                "attribution_modes": ["actor_scoped"],
            },
        ],
    )

    assert inspect_shard_plan(tmp_path, context_dir).errors == ()


def test_shard_plan_rejects_a_missing_context_item(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`](github/acme/one/overview.md)\n"
        "- **two** [attribution mode: `personal`](github/acme/two/overview.md)\n",
        encoding="utf-8",
    )
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
            }
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert "Context item 'github/acme/two' is not covered by any shard" in errors


def test_shard_plan_rejects_a_context_item_in_two_shards(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`](github/acme/one/overview.md)\n",
        encoding="utf-8",
    )
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "first",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
            },
            {
                "id": "second",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
            },
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert any("covered by multiple shards" in error for error in errors)


def test_shard_plan_rejects_an_unmatched_scope(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`](github/acme/one/overview.md)\n",
        encoding="utf-8",
    )
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/missing",
                "attribution_modes": ["personal"],
            }
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert "shard 'one' scope does not match Context items" in errors


def test_shard_plan_rejects_mismatched_attribution_modes(tmp_path: Path) -> None:
    context_dir = _single_context_item(tmp_path)
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert "shard 'one' attribution modes do not match Context" in errors


def test_shard_plan_rejects_a_non_canonical_report_path(tmp_path: Path) -> None:
    context_dir = _single_context_item(tmp_path)
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
                "report": "/work/shards/other.md",
            }
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert "shard 'one' has a non-canonical report path" in errors


def test_shard_plan_rejects_non_pending_status_with_exact_error(
    tmp_path: Path,
) -> None:
    context_dir = _single_context_item(tmp_path)
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
                "status": "complete",
            }
        ],
    )

    assert inspect_shard_plan(tmp_path, context_dir).errors == (
        "shard 'one' is not pending",
    )


def test_shard_plan_rejects_nonzero_retry_count_with_exact_error(
    tmp_path: Path,
) -> None:
    context_dir = _single_context_item(tmp_path)
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
                "retry_count": 1,
            }
        ],
    )

    assert inspect_shard_plan(tmp_path, context_dir).errors == (
        "shard 'one' retry_count must be 0 before dispatch",
    )


def test_post_run_validation_still_requires_terminal_status_and_report(
    tmp_path: Path,
) -> None:
    (tmp_path / "NOTES.md").write_text(
        '<!-- SHARD_STATUS_BEGIN -->{"shards":[{"id":"one",'
        '"status":"pending","retry_count":0,"report":"/work/shards/one.md"}]}'
        "<!-- SHARD_STATUS_END -->",
        encoding="utf-8",
    )

    errors = inspect_shards(tmp_path).errors

    assert "shard 'one' is not in a terminal state" in errors
    assert "shard 'one' canonical report is missing" in errors


def test_executable_shard_plan_success_has_exact_output(tmp_path: Path) -> None:
    context_dir = _single_context_item(tmp_path)
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
            }
        ],
    )

    result = _run_shard_validator(tmp_path, context_dir)

    assert result.returncode == 0
    assert result.stdout == "Shard plan validation passed.\n"
    assert result.stderr == ""


def test_executable_shard_plan_failure_prints_validation_errors(
    tmp_path: Path,
) -> None:
    context_dir = _single_context_item(tmp_path)
    _write_plan_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/missing",
                "attribution_modes": ["personal"],
            }
        ],
    )

    result = _run_shard_validator(tmp_path, context_dir)

    assert result.returncode != 0
    assert result.stdout == (
        "shard 'one' scope does not match Context items\n"
        "Context item 'github/acme/one' is not covered by any shard\n"
    )
    assert result.stderr == ""


def test_shard_validation_does_not_match_context_scope_prefixes(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **repo-tools** [attribution mode: `actor_scoped`](repo-tools/overview.md)\n"
        "- **repo** [attribution mode: `personal`](repo/overview.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "NOTES.md").write_text(
        '<!-- SHARD_STATUS_BEGIN -->{"shards":[{"id":"repo",'
        '"scope":"/context/repo","attribution_modes":["personal"],'
        '"status":"complete","retry_count":0,"report":"/work/shards/repo.md"},'
        '{"id":"repo-tools","scope":"/context/repo-tools",'
        '"attribution_modes":["actor_scoped"],"status":"complete",'
        '"retry_count":0,"report":"/work/shards/repo-tools.md"}]}'
        "<!-- SHARD_STATUS_END -->",
        encoding="utf-8",
    )
    (tmp_path / "shards").mkdir()
    (tmp_path / "shards/repo.md").write_text(
        "## User work\n\nnone\n\n## Context-only evidence\n\nnone\n",
        encoding="utf-8",
    )
    (tmp_path / "shards/repo-tools.md").write_text(
        "## User work\n\nnone\n\n## Context-only evidence\n\nnone\n",
        encoding="utf-8",
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_rejects_missing_context_item(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`]("
        "github/acme/one/issue/1/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/two/issue/2/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "github/acme/one/issue/{1}",
                "attribution_modes": ["personal"],
            }
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("not covered by any shard" in error for error in errors)


def test_shard_partition_rejects_partial_brace_scope(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/2/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "partial",
                "scope": "github/acme/repo/issue/{1}",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("issue/2" in error for error in errors)


def test_shard_partition_allows_full_brace_scope(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/2/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "all",
                "scope": "github/acme/repo/issue/{1,2}",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_allows_repository_broad_scope(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **issue-one** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **issue-two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/2/overview.md)\n"
        "- **pull-three** [attribution mode: `actor_scoped`]("
        "github/acme/repo/pull/3/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "repository",
                "scope": "github/acme/repo",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_rejects_brace_overlap(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/2/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "first",
                "scope": "github/acme/repo/issue/{1,2}",
                "attribution_modes": ["actor_scoped"],
            },
            {
                "id": "second",
                "scope": "github/acme/repo/issue/{2}",
                "attribution_modes": ["actor_scoped"],
            },
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("issue/2" in error for error in errors)
    assert any("covered by multiple shards" in error for error in errors)


def test_shard_partition_resolves_mixed_misc_brace_scope_exactly(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **issue-one** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **issue-two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/2/overview.md)\n"
        "- **session-one** [attribution mode: `personal`]("
        "opencode/work/project/session/01/overview.md)\n"
        "- **session-three** [attribution mode: `personal`]("
        "opencode/work/project/session/03/overview.md)\n"
        "- **session-five** [attribution mode: `personal`]("
        "opencode/work/project/session/05/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "misc",
                "scope": (
                    "misc: github/acme/repo/issue/{1,2}, "
                    "opencode/work/project/session/{01,03}"
                ),
                "attribution_modes": ["personal", "actor_scoped"],
            },
            {
                "id": "remaining",
                "scope": "opencode/work/project/session/{05}",
                "attribution_modes": ["personal"],
            },
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_rejects_overlapping_context_item(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`]("
        "github/acme/one/issue/1/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "first",
                "scope": "github/acme/one",
                "attribution_modes": ["personal"],
            },
            {
                "id": "second",
                "scope": "github/acme/one/issue/{1}",
                "attribution_modes": ["personal"],
            },
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("covered by multiple shards" in error for error in errors)


def test_shard_partition_allows_repository_scope_covering_multiple_items(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/pull/2/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "repo",
                "scope": "repository acme/repo",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_allows_misc_scope_covering_explicit_items(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`]("
        "opencode/home/work/session/01/overview.md)\n"
        "- **two** [attribution mode: `actor_scoped`]("
        "github/acme/repo/issue/1/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "misc",
                "scope": (
                    "misc: opencode/home/work/session/01, github/acme/repo/issue/1"
                ),
                "attribution_modes": ["personal", "actor_scoped"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def _single_context_item(tmp_path: Path) -> Path:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`](github/acme/one/overview.md)\n",
        encoding="utf-8",
    )
    return context_dir


def _write_plan_fixture(tmp_path: Path, shards: list[dict[str, object]]) -> None:
    entries = []
    for shard in shards:
        shard_id = str(shard["id"])
        entries.append(
            {
                **shard,
                "status": shard.get("status", "pending"),
                "retry_count": shard.get("retry_count", 0),
                "report": shard.get("report", f"/work/shards/{shard_id}.md"),
            }
        )
    (tmp_path / "NOTES.md").write_text(
        "<!-- SHARD_STATUS_BEGIN -->"
        + json.dumps({"shards": entries})
        + "<!-- SHARD_STATUS_END -->",
        encoding="utf-8",
    )
    (tmp_path / "shards").mkdir()


def _run_shard_validator(
    work_dir: Path, context_dir: Path
) -> subprocess.CompletedProcess[str]:
    validator = Path(__file__).parents[1] / "src/tracebase/summary_shard_validator.py"
    return subprocess.run(
        ["python3", str(validator), "plan", str(work_dir), str(context_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_partition_fixture(tmp_path: Path, shards: list[dict[str, object]]) -> None:
    (tmp_path / "NOTES.md").write_text(
        "<!-- SHARD_STATUS_BEGIN -->"
        + json.dumps(
            {
                "shards": [
                    {
                        **shard,
                        "status": "complete",
                        "retry_count": 0,
                        "report": f"/work/shards/{shard['id']}.md",
                    }
                    for shard in shards
                ]
            }
        )
        + "<!-- SHARD_STATUS_END -->",
        encoding="utf-8",
    )
    (tmp_path / "shards").mkdir()
    for shard in shards:
        (tmp_path / "shards" / f"{shard['id']}.md").write_text(
            "## User work\n\nnone\n\n## Context-only evidence\n\nnone\n",
            encoding="utf-8",
        )


def test_shard_validation_matches_context_scope_file_set(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **repo** [attribution mode: `actor_scoped`](github/repo/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "repo",
                "scope": "repository repo; PRs #1, #2",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_validation_matches_source_path_scopes(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **GitHub** [attribution mode: `actor_scoped`]("
        "github/example/project/pull/1/overview.md)\n"
        "- **OpenCode** [attribution mode: `personal`]("
        "opencode/home/tester/project/session/01/overview.md)\n",
        encoding="utf-8",
    )
    shards = [
        {
            "id": "github",
            "scope": "github/example/project/pull/{1}",
            "attribution_modes": ["actor_scoped"],
        },
        {
            "id": "opencode",
            "scope": "opencode/home/tester/project/session/{01}",
            "attribution_modes": ["personal"],
        },
    ]
    _write_partition_fixture(tmp_path, shards)

    assert inspect_shards(tmp_path, context_dir).errors == ()


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
