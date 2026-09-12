"""Production Context Output to work summary orchestration."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol

from .context import ContextRequest, generate_context
from .summary_audit import audit_exports
from .summary_container import ContainerMounts, ContainerRunner, ensure_image
from .summary_metrics import UsageMetrics, aggregate_metrics
from .summary_opencode import prepare_state
from .summary_sessions import (
    compaction_count,
    descendant_session_ids,
    parse_session_rows,
    root_compaction_count,
    root_session_id,
)
from .summary_shards import inspect_shards

IMAGE = "tracebase-opencode:1.18.29"
RECOVERY_PROMPT = (
    "Canonical result recovery: reread /work/TASK.md and /work/NOTES.md, "
    "inspect every declared /work/shards/*.md report, and write the complete "
    "canonical result to /results/summary.md. Use this same root session; do "
    "not start a new session or silently omit a shard."
)


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
        return not path.is_symlink() and path.is_file() and bool(_read_result(path))
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


def _json(text: str, message: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise SummaryError(message) from error


def _run_stage(
    stage: Path,
    runner: Runner,
    config: str,
    model: str,
    variant: str | None,
    result_path: Path,
) -> tuple[UsageMetrics, str, dict[str, dict[str, Any]], bool]:
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
    (stage / "stdout.jsonl").write_text(initial.stdout, encoding="utf-8")
    (stage / "stderr.log").write_text(initial.stderr, encoding="utf-8")
    root_id = root_session_id(initial.stdout)
    recovered = False
    if not _has_result(result_path):
        recovery = [*command[:-1], "--session", root_id, RECOVERY_PROMPT]
        resumed = runner.run(recovery, config)
        (stage / "root-recovery.stdout.jsonl").write_text(
            resumed.stdout, encoding="utf-8"
        )
        (stage / "root-recovery.stderr.log").write_text(
            resumed.stderr, encoding="utf-8"
        )
        recovered = True
        _write_json(
            stage / "root-recovery.json",
            {
                "attempted": True,
                "root_session_id": root_id,
                "result_present": _has_result(result_path),
            },
        )
    database = runner.run(
        [
            "--pure",
            "db",
            "SELECT id, parent_id, tokens_input, tokens_output, tokens_reasoning, "
            "tokens_cache_read, tokens_cache_write, cost FROM session",
            "--format",
            "json",
        ],
        config,
    )
    records = parse_session_rows(
        _json(database.stdout, "invalid OpenCode database output")
    )
    session_ids = descendant_session_ids(root_id, records)
    exports: dict[str, dict[str, Any]] = {}
    sessions = stage / "sessions"
    sessions.mkdir()
    for session_id in session_ids:
        stdout_path = sessions / f"{session_id}.stdout"
        exported = runner.run(["--pure", "export", session_id], config, stdout_path)
        stdout_path.write_text(exported.stdout, encoding="utf-8")
        (sessions / f"{session_id}.stderr.log").write_text(
            exported.stderr, encoding="utf-8"
        )
        value = _json(exported.stdout, f"export for {session_id} is invalid")
        if not isinstance(value, dict):
            raise SummaryError(f"export for {session_id} is not an object")
        exports[session_id] = value
        _write_json(sessions / f"{session_id}.json", value)
    _write_json(stage / "root-session.json", exports[root_id])
    return aggregate_metrics(session_ids, records, exports), root_id, exports, recovered


def _tracebase_version() -> str:
    try:
        return version("tracebase")
    except PackageNotFoundError:
        return "unknown"


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    return first == second or first in second.parents or second in first.parents


def summarize(request: SummaryRequest, runner: Runner | None = None) -> Path:
    """Run the canonical Summarizer and atomically publish its valid result."""
    target = request.output.absolute()
    if target.exists() or target.is_symlink():
        raise SummaryError("summary output already exists")
    if _paths_overlap(request.context, target):
        raise SummaryError("summary output must not overlap Context Output")
    # Fingerprint the immutable run-local snapshot, not the caller-owned source.
    fingerprint_context(request.context)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        context = staging / "context"
        shutil.copytree(request.context, context)
        fingerprint = fingerprint_context(context)
        work = staging / "work"
        results = staging / "results"
        state = staging / ".opencode-data"
        work.mkdir()
        results.mkdir()
        prompt = Path(__file__).parent / "prompts/summarizer-v1.md"
        task = work / "TASK.md"
        task.write_bytes(prompt.read_bytes())
        task_hash = hashlib.sha256(task.read_bytes()).hexdigest()
        config = prepare_state(state, request.model)
        dockerfile = Path(__file__).parent / "container/Dockerfile"
        if runner is None:
            ensure_image(IMAGE, dockerfile)
            runner = ContainerRunner(
                IMAGE, ContainerMounts(context, work, results, state, task=task)
            )
        opencode_version = runner.run(["--pure", "--version"], config).stdout.strip()
        metrics, root_id, exports, recovered = _run_stage(
            staging,
            runner,
            config,
            request.model,
            request.variant,
            results / "summary.md",
        )
        summary = _read_result(results / "summary.md")
        shards = inspect_shards(work, context)
        violations = audit_exports(exports)
        if shards.errors:
            raise SummaryError("Summarizer shard protocol was incomplete")
        if violations:
            raise SummaryError("Summarizer attempted to use prohibited evidence")
        (staging / "summary.md").write_text(summary, encoding="utf-8")
        manifest: dict[str, Any] = {
            "format_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "complete",
            "model": request.model,
            "variant": request.variant,
            "task_sha256": task_hash,
            "tracebase_version": _tracebase_version(),
            "opencode_version": opencode_version,
            "container_image": IMAGE,
            "requested_interval": request.requested_interval,
            "context_input": fingerprint,
            "root_session_id": root_id,
            "canonical_result_recovery": recovered,
            "compaction_count": compaction_count(exports),
            "root_compaction_count": root_compaction_count(root_id, exports),
            "shards": shards.as_dict(),
            "protocol_violations": [],
            "metrics": metrics.as_dict(),
        }
        _write_json(staging / "manifest.json", manifest)
        shutil.rmtree(state)
        shutil.rmtree(results)
        staging.rename(target)
        return target
    except SummaryError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        raise SummaryError("summary operation failed") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def summarize_archive(
    archive: Path,
    context_output: Path | None,
    context_request: ContextRequest,
    model: str,
    variant: str | None,
    output: Path,
) -> Path:
    """Compose archive extraction with production summarization."""
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
                {
                    "from": context_request.from_text,
                    "to": context_request.to_text,
                },
            )
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
