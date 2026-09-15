import json
import re
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
        missing_context_section_for: set[str] | None = None,
        repair_on_recovery: bool = False,
        repair_only_shards: set[str] | None = None,
        malformed_status_block: bool = False,
        shards: list[dict[str, object]] | None = None,
    ) -> None:
        self.output = output
        self.recover = recover
        self.incomplete = incomplete
        self.missing_context_section = missing_context_section
        self.missing_context_section_for = missing_context_section_for or set()
        self.repair_on_recovery = repair_on_recovery
        self.repair_only_shards = repair_only_shards
        self.malformed_status_block = malformed_status_block
        self.shards = shards
        self.calls: list[list[str]] = []
        self.report_writes: list[str] = []

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
                shard_specs = self.shards or [
                    {
                        "id": "repo",
                        "scope": "/context/repo",
                        "attribution_modes": ["personal"],
                    }
                ]
                written_specs = shard_specs
                if "--session" in arguments and self.repair_only_shards is not None:
                    written_specs = [
                        shard
                        for shard in shard_specs
                        if shard["id"] in self.repair_only_shards
                    ]
                for shard in written_specs:
                    shard_id = shard["id"]
                    assert isinstance(shard_id, str)
                    self.report_writes.append(shard_id)
                    (work / f"shards/{shard_id}.md").write_text(
                        "## User work\n\nevidence\n"
                        + (
                            ""
                            if (
                                self.missing_context_section
                                or shard_id in self.missing_context_section_for
                            )
                            and not (
                                "--session" in arguments and self.repair_on_recovery
                            )
                            else "\n## Context-only evidence\n\nNone.\n"
                        ),
                        encoding="utf-8",
                    )
                status = (
                    "failed"
                    if self.incomplete
                    and (
                        not ("--session" in arguments and self.repair_on_recovery)
                        or any(shard.get("retry_count") == 1 for shard in shard_specs)
                    )
                    else "complete"
                )
                notes = (
                    "<!-- SHARD_STATUS_BEGIN -->\n"
                    + json.dumps(
                        {
                            "shards": [
                                {
                                    **shard,
                                    "status": status,
                                    "retry_count": shard.get("retry_count", 0),
                                    "report": f"/work/shards/{shard['id']}.md",
                                }
                                for shard in shard_specs
                            ]
                        }
                    )
                    + "\n<!-- SHARD_STATUS_END -->\n"
                )
                if self.malformed_status_block and "--session" not in arguments:
                    notes = "root bookkeeping needs repair\n"
                (work / "NOTES.md").write_text(notes, encoding="utf-8")
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


def multi_source_context(tmp_path: Path) -> Path:
    result = tmp_path / "context"
    result.mkdir()
    (result / "index.md").write_text(
        "# Context Output\n\n"
        "Requested interval: `2026-01-01T01:00:00+01:00 <= t < "
        "2026-01-01T03:00:00+01:00`\n"
        "- **OpenCode session** [attribution mode: `personal`]("
        "opencode/home/work/project/session/01/overview.md)\n"
        "- **ChatGPT conversation** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-alpha/overview.md)\n",
        encoding="utf-8",
    )
    return result


def _write_single_chatgpt_index(context_dir: Path) -> None:
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **conversation** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-alpha/overview.md)\n",
        encoding="utf-8",
    )


def _write_chatgpt_index(context_dir: Path, *conversation_ids: str) -> None:
    context_dir.mkdir()
    lines = "".join(
        f"- **{conversation_id}** [attribution mode: `personal`]("
        f"chatgpt/conversation/{conversation_id}/overview.md)\n"
        for conversation_id in conversation_ids
    )
    (context_dir / "index.md").write_text(lines, encoding="utf-8")


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
    assert not any("--session" in call for call in runner.calls)


def test_summary_publishes_mixed_opencode_and_chatgpt_context_partition(
    tmp_path: Path,
) -> None:
    source = multi_source_context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        shards=[
            {
                "id": "opencode",
                "scope": "opencode/home/work/project/session/01",
                "attribution_modes": ["personal"],
            },
            {
                "id": "chatgpt",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            },
        ],
    )
    bind_runner_to_staging(runner, tmp_path)

    published = summarize(SummaryRequest(source, "model", None, output), runner)

    assert published == output
    assert (output / "summary.md").read_text() == "# Work summary\n"


def test_summary_rejects_mixed_context_when_chatgpt_item_is_not_sharded(
    tmp_path: Path,
) -> None:
    source = multi_source_context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        shards=[
            {
                "id": "opencode",
                "scope": "opencode/home/work/project/session/01",
                "attribution_modes": ["personal"],
            }
        ],
    )
    bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert not output.exists()


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
    normalized_prompt = " ".join(prompt.split())

    required_semantics = (
        r"`personal` is an attribution rule, not a work-relevance classification",
        r"Attribution and work relevance are separate judgments",
        r"purpose, intent, and activity character",
        r"Explicit work linkage:.*repository.*PR.*issue.*project",
        r"Intrinsic work intent:.*investigation, research, competitive analysis,"
        r" technical evaluation",
        r"Cross-source support:.*GitHub, OpenCode",
        r"Technical subject matter alone does not prove work intent",
        r"conversation length, message count, command volume, troubleshooting"
        r" complexity",
        r"Personal operational troubleshooting may qualify only when.*blocks"
        r" development or an engineering task",
        r"supporting work rather than an independent major workstream",
        r"association helps organization but is not a prerequisite for"
        r" work eligibility",
        r"standalone work/research",
        r"Do not guess or invent a project relationship",
        r"do not discard otherwise valid work because its project is unknown",
    )
    for semantic_clause in required_semantics:
        assert re.search(semantic_clause, normalized_prompt)

    assert "final Summary is a projection of the user's work" in prompt
    assert "`personal`" in prompt and "`actor_scoped`" in prompt
    assert "authoritative attribution" in prompt
    assert "same review thread" in prompt
    assert "single `## Commits` section" in prompt
    assert "## User work" in prompt
    assert "## Context-only evidence" in prompt
    assert "shard inventory covers every projected Context item" in normalized_prompt
    assert (
        "workstream inventory contains only materially meaningful work"
        in normalized_prompt
    )
    assert "User work: None" in normalized_prompt
    assert (
        "Each ChatGPT conversation is an independent projected Context item"
        in normalized_prompt
    )
    assert "exact canonical conversation roots" in normalized_prompt
    assert (
        "Do not create a cross-source relationship from time proximity"
        in normalized_prompt
    )
    assert "`activity.md`" in normalized_prompt
    assert "`background.md`" in normalized_prompt
    assert (
        "Background cannot independently create a requested-interval workstream"
        in normalized_prompt
    )
    assert "Later dialogue" in normalized_prompt
    assert "Assistant text itself does not prove" in normalized_prompt
    assert "assistant proposal" in normalized_prompt
    assert "implementation, execution, deployment" in normalized_prompt
    assert "validation happened" in normalized_prompt
    worker_contract = (
        "Worker Relevance Contract",
        "personal` is attribution, not work relevance",
        "Explicit project or workstream association may support relevance "
        "but is not required",
        "Technical subject matter, complexity, duration, interaction count, or",
        "troubleshooting depth do not by themselves establish work relevance",
        "Non-work personal activity belongs in `Context-only evidence`",
        "delegated cognitive work",
        "The worker does not decide Summary materiality",
        "Work-related does not mean it must appear in the final Summary",
        "must never create a new workstream",
        "never to rescue a worker's misclassification",
        "do not ask a worker to read the root `TASK.md`",
    )
    for clause in worker_contract:
        assert clause in normalized_prompt
    assert "analysis, research, investigation, review, evaluation" in prompt
    assert "assistant patch, command, or" in prompt
    assert "suggestion to run, test, or" in prompt
    assert "Only `User work` may be promoted" in prompt
    assert "Repository ownership" in prompt
    assert "Run the plan validator only during initial shard planning" in prompt
    assert "After worker dispatch begins, never run the plan validator again" in prompt
    assert "use the\nshard status and reports for completion reconciliation." in prompt


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


def test_shard_protocol_recovery_reuses_root_and_passes_validator_errors(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    debug = tmp_path / "debug"
    runner = FakeRunner(tmp_path, incomplete=True, repair_on_recovery=True)
    bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output, debug_output=debug), runner)

    recovery_calls = [call for call in runner.calls if "--session" in call]
    assert len(recovery_calls) == 1
    recovery_prompt = recovery_calls[0][-1]
    assert "Shard protocol validation failed." in recovery_prompt
    assert "shard 'repo' reported failure" in recovery_prompt
    assert recovery_calls[0][recovery_calls[0].index("--session") + 1] == "root"
    recovery = json.loads((debug / "runtime/root-recovery.json").read_text())
    assert recovery["validator_errors"] == ["shard 'repo' reported failure"]
    assert recovery["final_validator_errors"] == []


def test_root_status_recovery_reuses_root_and_passes_second_inspect(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path,
        malformed_status_block=True,
        repair_on_recovery=True,
    )
    bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output), runner)

    recovery_calls = [call for call in runner.calls if "--session" in call]
    assert len(recovery_calls) == 1
    assert "NOTES.md has no SHARD_STATUS block" in recovery_calls[0][-1]


def test_shard_protocol_recovery_only_repairs_invalid_shard(
    tmp_path: Path,
) -> None:
    source = multi_source_context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        missing_context_section_for={"chatgpt"},
        repair_on_recovery=True,
        repair_only_shards={"chatgpt"},
        shards=[
            {
                "id": "opencode",
                "scope": "opencode/home/work/project/session/01",
                "attribution_modes": ["personal"],
            },
            {
                "id": "chatgpt",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            },
        ],
    )
    bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output), runner)

    assert runner.report_writes == ["opencode", "chatgpt", "chatgpt"]
    recovery_prompt = next(call[-1] for call in runner.calls if "--session" in call)
    assert "shard 'chatgpt' report does not separate" in recovery_prompt
    assert "shard 'opencode'" not in recovery_prompt


def test_incomplete_shards_publish_nothing(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(tmp_path, incomplete=True)
    bind_runner_to_staging(runner, tmp_path)
    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert not output.exists()
    assert not list(tmp_path.glob(".summary.*"))
    assert len([call for call in runner.calls if "--session" in call]) == 1


def test_protocol_recovery_does_not_bypass_retry_limit(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path,
        incomplete=True,
        repair_on_recovery=True,
        shards=[
            {
                "id": "repo",
                "scope": "/context/repo",
                "attribution_modes": ["personal"],
                "retry_count": 1,
            }
        ],
    )
    bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    recovery_prompt = next(call[-1] for call in runner.calls if "--session" in call)
    assert "retry_count is already 1" in recovery_prompt
    assert len([call for call in runner.calls if "--session" in call]) == 1


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
    assert not (debug / "opencode").exists()
    assert "opencode" not in json.loads((debug / "manifest.json").read_text())
    assert len(runner.calls) == 2


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
    assert not (debug / "opencode").exists()
    assert (debug / "results/summary.md").read_text() == "# Work summary\n"
    assert json.loads((debug / "manifest.json").read_text())["status"] == "failed"
    assert len(runner.calls) == 3


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


@pytest.mark.parametrize("level", ["#", "##", "###", "######"])
def test_shard_report_accepts_any_atx_heading_level(tmp_path: Path, level: str) -> None:
    _write_partition_fixture(
        tmp_path, [{"id": "repo", "scope": "repo", "attribution_modes": ["personal"]}]
    )
    (tmp_path / "shards/repo.md").write_text(
        f"{level} User work\n\nevidence\n\n{level} Context-only evidence\n\nnone\n",
        encoding="utf-8",
    )

    assert inspect_shards(tmp_path).errors == ()


def test_shard_report_rejects_missing_normalized_heading_text(tmp_path: Path) -> None:
    _write_partition_fixture(
        tmp_path, [{"id": "repo", "scope": "repo", "attribution_modes": ["personal"]}]
    )
    (tmp_path / "shards/repo.md").write_text(
        "# User work\n\nevidence\n\n### Other evidence\n\nnone\n",
        encoding="utf-8",
    )

    errors = inspect_shards(tmp_path).errors

    assert any("does not separate" in error for error in errors)


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


def test_shard_partition_allows_exact_chatgpt_conversation_scope(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    _write_single_chatgpt_index(context_dir)
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "conversation",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_rejects_uncovered_chatgpt_conversation(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **one** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-alpha/overview.md)\n"
        "- **two** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-beta/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "one",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            }
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("cgpt-beta" in error and "not covered" in error for error in errors)


def test_shard_partition_rejects_overlapping_chatgpt_conversation(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    _write_single_chatgpt_index(context_dir)
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "first",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            },
            {
                "id": "second",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            },
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("covered by multiple shards" in error for error in errors)


def test_shard_partition_rejects_chatgpt_attribution_mismatch(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    _write_single_chatgpt_index(context_dir)
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "conversation",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["actor_scoped"],
            }
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("attribution modes do not match Context" in error for error in errors)


@pytest.mark.parametrize(
    "scope",
    ["/context/chatgpt", "/context/chatgpt/conversation"],
)
def test_shard_partition_rejects_broad_chatgpt_scope(
    tmp_path: Path, scope: str
) -> None:
    context_dir = tmp_path / "context"
    _write_chatgpt_index(context_dir, "foo", "bar")
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "chatgpt",
                "scope": scope,
                "attribution_modes": ["personal"],
            }
        ],
    )

    errors = inspect_shards(tmp_path, context_dir).errors

    assert any("scope does not match Context items" in error for error in errors)


def test_shard_partition_allows_exact_chatgpt_scope_without_broad_match(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    _write_chatgpt_index(context_dir, "foo", "bar")
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "foo",
                "scope": "/context/chatgpt/conversation/foo",
                "attribution_modes": ["personal"],
            },
            {
                "id": "bar",
                "scope": "/context/chatgpt/conversation/bar",
                "attribution_modes": ["personal"],
            },
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_exact_chatgpt_scope_rejects_similar_prefix(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    _write_chatgpt_index(context_dir, "foo", "foo-extra")
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "foo",
                "scope": "/context/chatgpt/conversation/foo",
                "attribution_modes": ["personal"],
            },
            {
                "id": "foo-extra",
                "scope": "/context/chatgpt/conversation/foo-extra",
                "attribution_modes": ["personal"],
            },
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_allows_explicit_chatgpt_misc_scope(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    _write_chatgpt_index(context_dir, "foo", "bar")
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "misc",
                "scope": ("misc: chatgpt/conversation/foo, chatgpt/conversation/bar"),
                "attribution_modes": ["personal"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_resolves_mixed_misc_chatgpt_scope_exactly(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **issue** [attribution mode: `actor_scoped`]("
        "github/acme/project/pull/1/overview.md)\n"
        "- **session** [attribution mode: `personal`]("
        "opencode/home/work/project/session/01/overview.md)\n"
        "- **conversation** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-alpha/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "misc",
                "scope": (
                    "misc: github/acme/project/pull/1, "
                    "opencode/home/work/project/session/01, "
                    "chatgpt/conversation/cgpt-alpha"
                ),
                "attribution_modes": ["actor_scoped", "personal"],
            }
        ],
    )

    assert inspect_shards(tmp_path, context_dir).errors == ()


def test_shard_partition_does_not_match_chatgpt_root_prefixes(
    tmp_path: Path,
) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "index.md").write_text(
        "- **short** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-alpha/overview.md)\n"
        "- **long** [attribution mode: `personal`]("
        "chatgpt/conversation/cgpt-alpha-extended/overview.md)\n",
        encoding="utf-8",
    )
    _write_partition_fixture(
        tmp_path,
        [
            {
                "id": "short",
                "scope": "chatgpt/conversation/cgpt-alpha",
                "attribution_modes": ["personal"],
            },
            {
                "id": "long",
                "scope": "chatgpt/conversation/cgpt-alpha-extended",
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
