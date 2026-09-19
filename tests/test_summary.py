import json
import subprocess
from pathlib import Path

import pytest

from tracebase import cli
from tracebase.context import ContextRequest
from tracebase.summary import (
    OPENCODE_VERSION,
    SummaryError,
    SummaryRequest,
    fingerprint_context,
    summarize,
)
from tracebase.summary_shards import inspect_shard_plan, inspect_shards


class FakeRunner:
    def __init__(
        self,
        output: Path,
        *,
        incomplete: bool = False,
        missing_context_section: bool = False,
        missing_context_section_for: set[str] | None = None,
        repair_on_recovery: bool = False,
        repair_only_shards: set[str] | None = None,
        missing_result: bool = False,
        shards: list[dict[str, object]] | None = None,
        mutate_items: bool = False,
    ) -> None:
        self.output = output
        self.incomplete = incomplete
        self.missing_context_section = missing_context_section
        self.missing_context_section_for = missing_context_section_for or set()
        self.repair_on_recovery = repair_on_recovery
        self.repair_only_shards = repair_only_shards
        self.missing_result = missing_result
        self.shards = shards
        self.mutate_items = mutate_items
        self.calls: list[list[str]] = []
        self.report_writes: list[str] = []
        self.root_saw_pending_plan = False

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
        if "run" not in arguments:
            raise AssertionError(f"unexpected runner call: {arguments}")

        work = self.output / "work"
        (work / "shards").mkdir(exist_ok=True)
        notes_text = (work / "NOTES.md").read_text(encoding="utf-8")
        status_json = notes_text.split("<!-- SHARD_STATUS_BEGIN -->", 1)[1].split(
            "<!-- SHARD_STATUS_END -->", 1
        )[0]
        host_specs = json.loads(status_json)["shards"]
        self.root_saw_pending_plan = all(
            shard["status"] == "pending" and shard["retry_count"] == 0
            for shard in host_specs
        )
        shard_specs = self.shards or host_specs
        if self.mutate_items:
            shard_specs = [
                {**shard, "items": ["mutated/item"]} for shard in shard_specs
            ]
        written_specs = shard_specs
        exhausted_retry = any(shard.get("retry_count") == 1 for shard in shard_specs)
        if "--session" in arguments and self.repair_only_shards is not None:
            written_specs = [
                shard for shard in shard_specs if shard["id"] in self.repair_only_shards
            ]
        if (
            "--session" in arguments
            and exhausted_retry
            and "do not start another worker" in arguments[-1]
        ):
            written_specs = []
        for shard in written_specs:
            shard_id = shard["id"]
            assert isinstance(shard_id, str)
            self.report_writes.append(shard_id)
            include_context = not (
                self.missing_context_section
                or shard_id in self.missing_context_section_for
            )
            if "--session" in arguments and self.repair_on_recovery:
                include_context = True
            report = "## User work\n\nevidence\n"
            if include_context:
                report += "\n## Context-only evidence\n\nNone.\n"
            (work / f"shards/{shard_id}.md").write_text(report, encoding="utf-8")

        retry = 1 if "--session" in arguments else 0
        if shard_specs and isinstance(shard_specs[0].get("retry_count"), int):
            retry = shard_specs[0]["retry_count"]
        status = "complete"
        if exhausted_retry or (
            self.incomplete
            and not ("--session" in arguments and self.repair_on_recovery)
        ):
            status = "failed"
        notes = {
            "shards": [
                {
                    **shard,
                    "status": status,
                    "retry_count": shard.get("retry_count", retry),
                    "report": f"/work/shards/{shard['id']}.md",
                }
                for shard in shard_specs
            ]
        }
        (work / "NOTES.md").write_text(
            "<!-- SHARD_STATUS_BEGIN -->"
            + json.dumps(notes)
            + "<!-- SHARD_STATUS_END -->",
            encoding="utf-8",
        )
        if not self.missing_result or (
            "--session" in arguments and self.repair_on_recovery
        ):
            (self.output / "results/summary.md").write_text(
                "# Work summary\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(arguments, 0, '{"sessionID":"root"}\n', "")


def _bind_runner_to_staging(runner: FakeRunner, parent: Path) -> None:
    original_run = runner.run

    def run(arguments: list[str], config: str, stdout_path: Path | None = None):
        candidates = list(parent.glob(".summary-run.*"))
        if candidates:
            runner.output = candidates[0]
        return original_run(arguments, config, stdout_path)

    runner.run = run  # type: ignore[method-assign]


def _write_context(
    tmp_path: Path,
    *items: tuple[str, str, str],
) -> Path:
    result = tmp_path / "context"
    result.mkdir()
    links = "".join(
        f"- **{label}** [attribution mode: `{mode}`]({root}/overview.md)\n"
        for root, label, mode in items
    )
    (result / "index.md").write_text(
        "# Context Output\n\n"
        "Requested interval: `2026-01-01T01:00:00+01:00 <= t < "
        "2026-01-01T03:00:00+01:00`\n" + links,
        encoding="utf-8",
    )
    inventory = []
    for root, label, mode in items:
        item_root = result.joinpath(*root.split("/"))
        item_root.mkdir(parents=True)
        (item_root / "overview.md").write_text(label, encoding="utf-8")
        inventory.append(
            {
                "root": root,
                "attribution_mode": mode,
                "files": ["overview.md"],
            }
        )
    (result / "index.json").write_text(
        json.dumps({"items": inventory}, indent=2) + "\n", encoding="utf-8"
    )
    return result


def context(tmp_path: Path) -> Path:
    return _write_context(tmp_path, ("repo", "repo", "personal"))


def multi_source_context(tmp_path: Path) -> Path:
    return _write_context(
        tmp_path,
        ("github/example/project/pull/1", "PR", "actor_scoped"),
        ("opencode/home/work/project/session/01", "session", "personal"),
        ("chatgpt/conversation/alpha", "conversation", "personal"),
    )


def _status(
    shards: list[dict[str, object]], *, status: str = "complete", retry_count: int = 0
) -> dict[str, object]:
    return {
        "shards": [
            {
                **shard,
                "status": shard.get("status", status),
                "retry_count": shard.get("retry_count", retry_count),
                "report": shard.get("report", f"/work/shards/{shard['id']}.md"),
            }
            for shard in shards
        ]
    }


def _write_status(
    work_dir: Path,
    shards: list[dict[str, object]],
    *,
    status: str = "complete",
    retry_count: int = 0,
) -> None:
    (work_dir / "NOTES.md").write_text(
        "<!-- SHARD_STATUS_BEGIN -->"
        + json.dumps(_status(shards, status=status, retry_count=retry_count))
        + "<!-- SHARD_STATUS_END -->",
        encoding="utf-8",
    )
    (work_dir / "shards").mkdir(exist_ok=True)
    for shard in shards:
        (work_dir / "shards" / f"{shard['id']}.md").write_text(
            "## User work\n\nNone.\n\n## Context-only evidence\n\nNone.\n",
            encoding="utf-8",
        )


def _write_plan(
    tmp_path: Path,
    shards: list[dict[str, object]],
    *,
    status: str = "pending",
    retry_count: int = 0,
) -> None:
    _write_status(tmp_path, shards, status=status, retry_count=retry_count)


def _run_validator(
    work_dir: Path, context_dir: Path
) -> subprocess.CompletedProcess[str]:
    validator = Path(__file__).parents[1] / "src/tracebase/summary_shard_validator.py"
    return subprocess.run(
        ["python3", str(validator), "plan", str(work_dir), str(context_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_existing_context_publishes_canonical_summary_and_provenance(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(output.parent / ".unused")
    _bind_runner_to_staging(runner, tmp_path)

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
    assert manifest["context_input"] == fingerprint_context(source)


def test_host_creates_pending_plan_before_root_starts(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(output.parent / ".unused")
    _bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output), runner)

    assert runner.root_saw_pending_plan


def test_root_item_mutation_is_rejected_during_final_reconciliation(
    tmp_path: Path,
) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(output.parent / ".unused", mutate_items=True)
    _bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="changed its host-assigned item list"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert not output.exists()


def test_summary_accepts_cross_source_batch(tmp_path: Path) -> None:
    source = multi_source_context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(tmp_path / ".unused")
    _bind_runner_to_staging(runner, tmp_path)

    assert summarize(SummaryRequest(source, "model", None, output), runner) == output


def test_summary_rejects_uncovered_context_item(tmp_path: Path) -> None:
    source = multi_source_context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        shards=[{"id": "one", "items": ["chatgpt/conversation/alpha"]}],
    )
    _bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert not output.exists()


def test_summarizer_contract_describes_generic_partitioning() -> None:
    prompt = (
        Path(__file__).parents[1] / "src/tracebase/prompts/summarizer-v1.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())
    required = (
        "Host-created shard plan",
        "existing host-created `/work/NOTES.md`",
        "Do not create or recreate the",
        "Maintain the existing host-created `SHARD_STATUS` block",
        "Tracebase has already generated and validated the complete shard plan",
        "The complete plan is frozen before dispatch",
        "Preserve every host-assigned shard ID, exact `items` list, and report path",
        "Do not re-plan, split, merge, rename, remove, or otherwise change "
        "shard membership",
        "Dispatch exactly the host-created shards",
        "Modify only `status` and `retry_count`",
        "Shard membership is an execution-only partition",
        "Worker Relevance Contract",
        "activity.md` contains the only dialogue eligible",
        "background.md` is earlier supporting context only",
        "Any historical PR, commit, implementation, or other work found only in",
        "keep the earlier work itself as background context only",
    )
    for clause in required:
        assert clause in normalized
    assert "For `actor_scoped`, follow the explicit `[User work]`" in normalized


def test_recovery_reuses_root_and_preserves_item_assignment(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    debug = tmp_path / "debug"
    runner = FakeRunner(
        tmp_path / ".unused",
        incomplete=True,
        repair_on_recovery=True,
    )
    _bind_runner_to_staging(runner, tmp_path)

    summarize(SummaryRequest(source, "model", None, output, debug_output=debug), runner)

    recovery_calls = [call for call in runner.calls if "--session" in call]
    assert len(recovery_calls) == 1
    recovery_prompt = recovery_calls[0][-1]
    assert "same assigned items" in recovery_prompt
    assert "same assigned scope" not in recovery_prompt
    assert "retry_count is already 1" in recovery_prompt
    assert "do not start another worker" in recovery_prompt


def test_missing_result_recovery_publishes_summary(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        missing_result=True,
        repair_on_recovery=True,
    )
    _bind_runner_to_staging(runner, tmp_path)

    assert summarize(SummaryRequest(source, "model", None, output), runner) == output
    assert len([call for call in runner.calls if "--session" in call]) == 1


def test_failed_shard_after_recovery_does_not_publish(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(tmp_path / ".unused", incomplete=True)
    _bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert not output.exists()


def test_exhausted_retry_does_not_start_another_worker(tmp_path: Path) -> None:
    source = context(tmp_path)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        incomplete=True,
        repair_on_recovery=True,
        shards=[{"id": "shard-01", "items": ["repo"], "retry_count": 1}],
    )
    _bind_runner_to_staging(runner, tmp_path)

    with pytest.raises(SummaryError, match="shard protocol"):
        summarize(SummaryRequest(source, "model", None, output), runner)

    assert runner.report_writes == ["shard-01"]
    assert not output.exists()


def test_recovery_can_repair_only_the_invalid_shard(tmp_path: Path) -> None:
    source = _write_context(
        tmp_path,
        ("one", "one", "personal"),
        ("two", "two", "personal"),
    )
    (source / "one/activity.md").write_bytes(b"x" * 40000)
    (source / "two/activity.md").write_bytes(b"x" * 40000)
    output = tmp_path / "summary"
    runner = FakeRunner(
        tmp_path / ".unused",
        missing_context_section_for={"shard-01"},
        repair_on_recovery=True,
        repair_only_shards={"shard-01"},
        shards=[
            {"id": "shard-01", "items": ["one"]},
            {"id": "shard-02", "items": ["two"]},
        ],
    )
    _bind_runner_to_staging(runner, tmp_path)

    assert summarize(SummaryRequest(source, "model", None, output), runner) == output
    assert runner.report_writes == ["shard-01", "shard-02", "shard-01"]


def test_retry_limit_is_rejected(tmp_path: Path) -> None:
    _write_status(
        tmp_path,
        [{"id": "one", "items": ["synthetic/new/item"]}],
        retry_count=2,
    )
    assert any(
        "retried more than once" in error for error in inspect_shards(tmp_path).errors
    )


def test_report_requires_both_evidence_sections(tmp_path: Path) -> None:
    _write_status(tmp_path, [{"id": "one", "items": ["synthetic/new/item"]}])
    (tmp_path / "shards/one.md").write_text(
        "## User work\n\nevidence\n", encoding="utf-8"
    )

    assert any(
        "does not separate" in error for error in inspect_shards(tmp_path).errors
    )


@pytest.mark.parametrize("level", ["#", "##", "###", "######"])
def test_report_accepts_any_atx_heading_level(tmp_path: Path, level: str) -> None:
    _write_status(tmp_path, [{"id": "one", "items": ["synthetic/new/item"]}])
    (tmp_path / "shards/one.md").write_text(
        f"{level} User work\n\nevidence\n\n{level} Context-only evidence\n\nnone\n",
        encoding="utf-8",
    )

    assert inspect_shards(tmp_path).errors == ()


def test_plan_accepts_exact_cross_source_partition(tmp_path: Path) -> None:
    context_dir = _write_context(
        tmp_path,
        ("synthetic/new-source/item-a", "new", "personal"),
        ("github/example/repo/pull/1", "github", "actor_scoped"),
        ("opencode/project/session/1", "opencode", "personal"),
        ("chatgpt/conversation/one", "chatgpt", "personal"),
        ("chatgpt/conversation/two", "chatgpt", "personal"),
    )
    shards = [
        {
            "id": "mixed",
            "items": [
                "synthetic/new-source/item-a",
                "github/example/repo/pull/1",
                "opencode/project/session/1",
            ],
        },
        {
            "id": "conversations",
            "items": ["chatgpt/conversation/one", "chatgpt/conversation/two"],
        },
    ]
    _write_plan(tmp_path, shards)

    assert inspect_shard_plan(tmp_path, context_dir).errors == ()


def test_plan_accepts_directory_subtree_split_without_semantic_claim(
    tmp_path: Path,
) -> None:
    context_dir = _write_context(
        tmp_path,
        ("github/example/repo/issue/1", "one", "actor_scoped"),
        ("github/example/repo/issue/2", "two", "actor_scoped"),
    )
    _write_plan(
        tmp_path,
        [
            {"id": "first", "items": ["github/example/repo/issue/1"]},
            {"id": "second", "items": ["github/example/repo/issue/2"]},
        ],
    )

    assert inspect_shard_plan(tmp_path, context_dir).errors == ()


@pytest.mark.parametrize(
    ("shards", "expected"),
    [
        ([{"id": "one", "items": []}], "items must be a non-empty list"),
        ([{"id": "one", "items": [""]}], "malformed item"),
        ([{"id": "one", "items": ["unknown/item"]}], "unknown Context item"),
        ([{"id": "one", "items": ["repo", "repo"]}], "more than once"),
    ],
)
def test_plan_rejects_malformed_or_unknown_items(
    tmp_path: Path, shards: list[dict[str, object]], expected: str
) -> None:
    context_dir = context(tmp_path)
    _write_plan(tmp_path, shards)

    assert any(
        expected in error for error in inspect_shard_plan(tmp_path, context_dir).errors
    )


def test_plan_rejects_duplicate_membership_and_missing_item(tmp_path: Path) -> None:
    context_dir = _write_context(
        tmp_path,
        ("one", "one", "personal"),
        ("two", "two", "personal"),
    )
    _write_plan(
        tmp_path,
        [
            {"id": "first", "items": ["one"]},
            {"id": "second", "items": ["one"]},
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert any("covered by multiple shards" in error for error in errors)
    assert any("'two' is not covered" in error for error in errors)


def test_plan_rejects_duplicate_ids_obsolete_fields_and_bad_report(
    tmp_path: Path,
) -> None:
    context_dir = context(tmp_path)
    _write_plan(
        tmp_path,
        [
            {
                "id": "one",
                "items": ["repo"],
                "scope": "repo",
                "attribution_modes": ["personal"],
                "report": "/work/shards/wrong.md",
            },
            {"id": "one", "items": ["repo"]},
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert any("declared more than once" in error for error in errors)
    assert any("unknown fields" in error for error in errors)
    assert any("attribution_modes" in error and "scope" in error for error in errors)
    assert any("non-canonical report path" in error for error in errors)


def test_plan_requires_pending_zero_retry_and_canonical_reports(tmp_path: Path) -> None:
    context_dir = context(tmp_path)
    _write_plan(tmp_path, [{"id": "one", "items": ["repo"]}], status="complete")

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert "shard 'one' is not pending" in errors
    assert "shard 'one' retry_count must be 0 before dispatch" not in errors

    _write_plan(tmp_path, [{"id": "one", "items": ["repo"]}], retry_count=1)
    assert any(
        "retry_count must be 0 before dispatch" in error
        for error in inspect_shard_plan(tmp_path, context_dir).errors
    )

    _write_plan(
        tmp_path,
        [{"id": "one", "items": ["repo"], "retry_count": 0.0}],
    )
    assert any(
        "retry_count must be 0 before dispatch" in error
        for error in inspect_shard_plan(tmp_path, context_dir).errors
    )


def test_plan_rejects_more_than_eight_items_in_one_shard(tmp_path: Path) -> None:
    items = [(f"item-{index}", str(index), "personal") for index in range(9)]
    context_dir = _write_context(tmp_path, *items)
    _write_plan(tmp_path, [{"id": "large", "items": [item[0] for item in items]}])

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert any("more than 8 Context items" in error for error in errors)


def test_plan_applies_generic_byte_limit_and_allows_one_oversized_item(
    tmp_path: Path,
) -> None:
    context_dir = _write_context(
        tmp_path,
        ("one", "one", "personal"),
        ("two", "two", "personal"),
        ("huge", "huge", "personal"),
    )
    (context_dir / "one/activity.md").write_bytes(b"x" * 200000)
    (context_dir / "two/activity.md").write_bytes(b"x" * 100000)
    (context_dir / "huge/activity.md").write_bytes(b"x" * 300000)

    _write_plan(
        tmp_path,
        [
            {"id": "too-large", "items": ["one", "two"]},
            {"id": "oversized", "items": ["huge"]},
        ],
    )

    errors = inspect_shard_plan(tmp_path, context_dir).errors

    assert any("exceeds 65536 readable bytes" in error for error in errors)
    assert not any("oversized" in error for error in errors)


def test_final_validation_requires_complete_status_report_and_partition(
    tmp_path: Path,
) -> None:
    context_dir = _write_context(
        tmp_path,
        ("one", "one", "personal"),
        ("two", "two", "personal"),
    )
    _write_status(tmp_path, [{"id": "one", "items": ["one"]}], status="pending")

    errors = inspect_shards(tmp_path, context_dir).errors

    assert "shard 'one' is not in a terminal state" in errors
    assert "Context item 'two' is not covered by any shard" in errors

    _write_status(tmp_path, [{"id": "one", "items": ["one"]}], status="failed")
    errors = inspect_shards(tmp_path, context_dir).errors
    assert "shard 'one' reported failure" in errors
    assert "shard 'one' is not in a terminal state" not in errors


def test_executable_validator_has_stable_output(tmp_path: Path) -> None:
    context_dir = context(tmp_path)
    _write_plan(tmp_path, [{"id": "one", "items": ["repo"]}])

    result = _run_validator(tmp_path, context_dir)

    assert result.returncode == 0
    assert result.stdout == "Shard plan validation passed.\n"
    assert result.stderr == ""


def test_executable_validator_reports_unknown_item(tmp_path: Path) -> None:
    context_dir = context(tmp_path)
    _write_plan(tmp_path, [{"id": "one", "items": ["github/example/missing"]}])

    result = _run_validator(tmp_path, context_dir)

    assert result.returncode == 1
    assert "unknown Context item" in result.stdout
    assert "repo" in result.stdout


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


def test_cli_archive_mode_composes_summary(tmp_path: Path, monkeypatch) -> None:
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
    assert calls[0][2] == ContextRequest.parse(
        "2026-01-01T01:00:00+01:00", "2026-01-01T03:00:00+01:00"
    )
