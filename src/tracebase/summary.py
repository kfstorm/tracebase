"""Production Context Output to work summary orchestration."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol

from .context import ContextRequest, generate_context
from .summary_container import ContainerMounts, ContainerRunner, ensure_image
from .summary_opencode import prepare_config, prepare_state
from .summary_planner import plan_shards, write_initial_plan
from .summary_shards import inspect_shard_plan, inspect_shards

OPENCODE_VERSION = "1.18.29"
IMAGE = f"tracebase-opencode:{OPENCODE_VERSION}"
SHARD_RECOVERY_PROMPT = """Shard protocol validation failed.

Reread /work/TASK.md and /work/NOTES.md. Fix only the shard-protocol
errors listed below, preserving existing valid investigation and evidence.

<VALIDATOR_ERRORS>

Classify each error before acting:

- If it concerns root-owned orchestration state such as the shard status
  block in /work/NOTES.md, repair it yourself.
- If an individual shard report is missing or substantively incomplete,
  recover only that shard. Prefer continuing the existing worker/session
  when the task mechanism supports it; otherwise retry that exact shard
  once with the same assigned items.
- Do not re-investigate valid shards.
- Do not change shard item assignments merely to satisfy validation.
- Do not omit or merge a failed shard.
- Do not rewrite valid report content except where required to restore
  the protocol.
- Do not change /results/summary.md except when the repaired shard evidence
  materially requires final synthesis to change.
- A shard whose retry_count is already 1 has exhausted its worker retry;
  do not start another worker for it, and preserve its failed status.

After repairs, reconcile the complete shard inventory and status block,
then ensure the required result exists at /results/summary.md."""
_INTERVAL = re.compile(r"^Requested interval: `(.+?) <= t < (.+?)`$", re.MULTILINE)


class SummaryError(RuntimeError):
    """Raised when a valid complete Summary Output cannot be produced."""


class Runner(Protocol):
    def run(
        self,
        arguments: list[str],
        config: str,
        stdout_path: Path | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    context: Path
    model: str
    variant: str | None
    output: Path
    requested_interval: dict[str, str] | None = None
    debug_output: Path | None = None


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_result(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SummaryError("Summarizer result /results/summary.md is missing")
    try:
        result = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SummaryError("could not read Summarizer result") from error
    if not result.strip():
        raise SummaryError("Summarizer result /results/summary.md is empty")
    return result


def _has_result(path: Path) -> bool:
    try:
        return bool(_read_result(path))
    except SummaryError:
        return False


def fingerprint_context(context: Path) -> dict[str, Any]:
    """Fingerprint a symlink-free Context Output by paths and bytes."""
    if context.is_symlink() or not context.is_dir():
        raise SummaryError("Context Output path must be a regular directory")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(context.rglob("*")):
        if path.is_symlink():
            raise SummaryError("Context Output must not contain symlinks")
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError as error:
            raise SummaryError("could not read Context Output") from error
        relative = path.relative_to(context).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        file_count += 1
        total_bytes += len(content)
    return {
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def context_interval(context: Path) -> dict[str, str]:
    """Read and validate the authoritative interval from Context index.md."""
    try:
        index = (context / "index.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SummaryError(
            "could not read requested interval from Context Output"
        ) from error
    match = _INTERVAL.search(index)
    if match is None:
        raise SummaryError("Context Output does not declare a requested interval")
    try:
        request = ContextRequest.parse(match.group(1), match.group(2))
    except ValueError as error:
        raise SummaryError("Context Output requested interval is invalid") from error
    return {"from": request.from_text, "to": request.to_text}


def _root_session_id(output: str) -> str:
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise SummaryError("OpenCode run output is not valid JSONL") from error
        if not isinstance(event, dict):
            continue
        candidates = [event]
        if isinstance(event.get("part"), dict):
            candidates.append(event["part"])
        for candidate in candidates:
            for key in ("sessionID", "session_id", "sessionId"):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    return value
    raise SummaryError("OpenCode run did not emit a root session ID")


def _run_stage(
    runtime: Path,
    runner: Runner,
    config: str,
    model: str,
    variant: str | None,
) -> tuple[str, list[str]]:
    command = [
        "--pure",
        "run",
        "--format",
        "json",
        "--model",
        model,
        "--dir",
        "/work",
        "--auto",
    ]
    if variant:
        command.extend(["--variant", variant])
    command.append("Read /work/TASK.md and complete the task described there.")
    initial = runner.run(command, config)
    (runtime / "stdout.jsonl").write_text(initial.stdout, encoding="utf-8")
    (runtime / "stderr.log").write_text(initial.stderr, encoding="utf-8")
    return _root_session_id(initial.stdout), command


def _recovery_prompt(errors: tuple[str, ...], result_missing: bool) -> str:
    details = list(errors)
    if result_missing:
        details.append("Summarizer result /results/summary.md is missing or empty")
    return SHARD_RECOVERY_PROMPT.replace(
        "<VALIDATOR_ERRORS>", "\n".join(f"- {error}" for error in details)
    )


def _run_recovery(
    runtime: Path,
    runner: Runner,
    config: str,
    command: list[str],
    root_id: str,
    errors: tuple[str, ...],
    result_missing: bool,
    result_path: Path,
    work: Path,
    context: Path,
    expected_items: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], bool]:
    recovery = [
        *command[:-1],
        "--session",
        root_id,
        _recovery_prompt(errors, result_missing),
    ]
    resumed = runner.run(recovery, config)
    (runtime / "root-recovery.stdout.jsonl").write_text(
        resumed.stdout, encoding="utf-8"
    )
    (runtime / "root-recovery.stderr.log").write_text(resumed.stderr, encoding="utf-8")
    final_errors = inspect_shards(work, context, expected_items=expected_items).errors
    final_result_present = _has_result(result_path)
    _write_json(
        runtime / "root-recovery.json",
        {
            "attempted": True,
            "root_session_id": root_id,
            "validator_errors": list(errors),
            "final_validator_errors": list(final_errors),
            "result_present": not result_missing,
            "final_result_present": final_result_present,
        },
    )
    return final_errors, final_result_present


def _tracebase_version() -> str:
    try:
        return version("tracebase")
    except PackageNotFoundError:
        return "unknown"


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    return first == second or first in second.parents or second in first.parents


def _validate_target(target: Path, label: str) -> None:
    if target.exists() or target.is_symlink():
        raise SummaryError(f"{label} already exists")


def _publish_directory(staging: Path, target: Path, label: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.rename(target)
    except FileExistsError:
        raise SummaryError(f"{label} already exists") from None
    except OSError as error:
        raise SummaryError(f"{label} publication failed") from error
    return target


def _publish_debug(
    run: Path,
    target: Path,
    provenance: dict[str, Any],
    status: str,
    error: str | None = None,
) -> None:
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for name in ("context", "work", "runtime", "results"):
            source = run / name
            if source.is_dir():
                shutil.copytree(source, staging / name)
        manifest = {
            **provenance,
            "status": status,
        }
        if error is not None:
            manifest["error"] = error
        _write_json(staging / "manifest.json", manifest)
        _publish_directory(staging, target, "debug output")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def summarize(request: SummaryRequest, runner: Runner | None = None) -> Path:
    """Run the canonical Summarizer and atomically publish its valid result."""
    target = request.output.absolute()
    debug_target = request.debug_output.absolute() if request.debug_output else None
    _validate_target(target, "summary output")
    if _paths_overlap(request.context, target):
        raise SummaryError("summary output must not overlap Context Output")
    if debug_target is not None:
        _validate_target(debug_target, "debug output")
        if _paths_overlap(request.context, debug_target) or _paths_overlap(
            target, debug_target
        ):
            raise SummaryError("debug output must not overlap other outputs")
    fingerprint_context(request.context)
    target.parent.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=".summary-run.", dir=target.parent))
    provenance: dict[str, Any] = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "model": request.model,
        "variant": request.variant,
        "tracebase_version": _tracebase_version(),
    }
    try:
        context = run / "context"
        shutil.copytree(request.context, context)
        provenance["context_input"] = fingerprint_context(context)
        provenance["requested_interval"] = (
            request.requested_interval or context_interval(context)
        )
        work = run / "work"
        results = run / "results"
        runtime = run / "runtime"
        state = run / ".opencode-data"
        work.mkdir()
        results.mkdir()
        runtime.mkdir()
        prompt = Path(__file__).parent / "prompts/summarizer-v1.md"
        task = work / "TASK.md"
        task.write_bytes(prompt.read_bytes())
        provenance["task_sha256"] = hashlib.sha256(task.read_bytes()).hexdigest()
        try:
            shard_plan = plan_shards(context)
            write_initial_plan(work, shard_plan)
        except ValueError as error:
            raise SummaryError(
                f"Context inventory or shard planning failed: {error}"
            ) from None
        plan_errors = inspect_shard_plan(work, context).errors
        if plan_errors:
            raise SummaryError(
                "Tracebase generated an invalid shard plan: " + "; ".join(plan_errors)
            )
        provenance["shard_plan"] = [
            {
                "id": shard.id,
                "items": list(shard.items),
                "readable_bytes": shard.readable_bytes,
            }
            for shard in shard_plan
        ]
        expected_items = {shard.id: shard.items for shard in shard_plan}
        dockerfile = Path(__file__).parent / "container/Dockerfile"
        if runner is None:
            config = prepare_state(state, request.model)
            ensure_image(IMAGE, dockerfile, OPENCODE_VERSION)
            runner = ContainerRunner(
                IMAGE,
                ContainerMounts(
                    context,
                    work,
                    results,
                    state,
                    task=task,
                ),
            )
        else:
            config = prepare_config(request.model)
        provenance["opencode_version"] = runner.run(
            ["--pure", "--version"], config
        ).stdout.strip()
        root_id, command = _run_stage(
            runtime, runner, config, request.model, request.variant
        )
        validation_errors = inspect_shards(
            work, context, expected_items=expected_items
        ).errors
        result_missing = not _has_result(results / "summary.md")
        if validation_errors or result_missing:
            validation_errors, result_present = _run_recovery(
                runtime,
                runner,
                config,
                command,
                root_id,
                validation_errors,
                result_missing,
                results / "summary.md",
                work,
                context,
                expected_items,
            )
            if not result_present:
                raise SummaryError("Summarizer result /results/summary.md is missing")
            if validation_errors:
                raise SummaryError(
                    "Summarizer shard protocol was incomplete: "
                    + "; ".join(validation_errors)
                )
        summary = _read_result(results / "summary.md")
        publication = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
        )
        try:
            (publication / "summary.md").write_text(summary, encoding="utf-8")
            _write_json(publication / "manifest.json", provenance)
            _publish_directory(publication, target, "summary output")
        finally:
            if publication.exists():
                shutil.rmtree(publication, ignore_errors=True)
        if debug_target is not None:
            debug_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                _publish_debug(run, debug_target, provenance, "complete")
            except OSError, SummaryError:
                shutil.rmtree(target, ignore_errors=True)
                raise SummaryError("debug output publication failed") from None
        return target
    except SummaryError as error:
        if debug_target is not None and not debug_target.exists():
            try:
                debug_target.parent.mkdir(parents=True, exist_ok=True)
                _publish_debug(run, debug_target, provenance, "failed", str(error))
            except OSError, SummaryError:
                pass
        raise
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        summary_error = SummaryError("summary operation failed")
        if debug_target is not None and not debug_target.exists():
            try:
                debug_target.parent.mkdir(parents=True, exist_ok=True)
                _publish_debug(
                    run, debug_target, provenance, "failed", str(summary_error)
                )
            except OSError, SummaryError:
                pass
        raise summary_error from error
    finally:
        shutil.rmtree(run, ignore_errors=True)


def summarize_archive(
    archive: Path,
    context_output: Path | None,
    context_request: ContextRequest,
    model: str,
    variant: str | None,
    output: Path,
    debug_output: Path | None = None,
) -> Path:
    """Compose archive extraction with production summarization."""
    outputs = [output]
    if debug_output is not None:
        outputs.append(debug_output)
    if context_output is not None:
        outputs.append(context_output)
    if any(_paths_overlap(archive, candidate) for candidate in outputs):
        raise SummaryError("summary outputs must not overlap Raw Archive")
    if (
        context_output is not None
        and debug_output is not None
        and _paths_overlap(context_output, debug_output)
    ):
        raise SummaryError("debug output must not overlap retained Context Output")
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if context_output is None:
            temporary = tempfile.TemporaryDirectory()
            context = Path(temporary.name) / "context"
        else:
            context = context_output
            if _paths_overlap(context, output):
                raise SummaryError(
                    "summary output must not overlap retained Context Output"
                )
        generate_context(archive, context_request, context)
        return summarize(
            SummaryRequest(
                context,
                model,
                variant,
                output,
                {"from": context_request.from_text, "to": context_request.to_text},
                debug_output,
            )
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
