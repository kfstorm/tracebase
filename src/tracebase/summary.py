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
from urllib.error import URLError
from urllib.request import Request, urlopen

from .context import ContextRequest, generate_context
from .context_inventory import (
    ContextInventoryError,
    load_context_inventory,
    materialize_context_evidence,
)
from .mutable_state import (
    MutableStateError,
    load_mutable_state,
    reconcile_mutable_state,
    render_mutable_state,
)
from .summary_container import (
    ContainerError,
    ContainerMounts,
    ContainerRunner,
    ensure_image,
)
from .summary_opencode import prepare_config, prepare_state
from .summary_planner import PlannedShard, plan_shards, write_initial_plan
from .summary_shards import inspect_shard_plan, inspect_shards

_EXACT_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_MAX_DOCKER_TAG_LENGTH = 128
_MAX_OUTPUT_LANGUAGE_LENGTH = 35
# Keep the primary subtag short enough to reject language names without a registry.
_LANGUAGE_SUBTAG = re.compile(r"[A-Za-z]{2,3}")
_SCRIPT_SUBTAG = re.compile(r"[A-Za-z]{4}")
_REGION_SUBTAG = re.compile(r"(?:[A-Za-z]{2}|[0-9]{3})")
_VARIANT_SUBTAG = re.compile(r"(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3})")
SHARD_RECOVERY_PROMPT = """Shard protocol validation failed.

Reread /work/TASK.md and /work/NOTES.md. Fix only the shard-protocol errors
listed below, preserving existing valid investigation and evidence.

<VALIDATOR_ERRORS>

Classify each error before acting:

- If it concerns root-owned orchestration state such as a shard STATUS.json,
  repair it yourself.
- If an individual shard report is missing or substantively incomplete,
  dispatch or continue only that shard's worker. The worker is the sole
  creator and modifier of REPORT.md; the root must never repair it directly.
  Retry that exact shard once with the same task and assignment.
- Do not re-investigate valid shards.
- Do not change shard item assignments merely to satisfy validation.
- Do not omit or merge a failed shard.
- Do not rewrite or delete any report content. The worker must restore its own
  report when the protocol requires it.
- Do not change /results/summary.md except when the repaired shard evidence
  materially requires final synthesis to change.
- A shard whose retry_count is already 1 has exhausted its worker retry;
  do not start another worker for it, and preserve its failed status.

After repairs, reconcile the complete shard inventory and statuses, then ensure
the required result exists at /results/summary.md."""


class SummaryError(RuntimeError):
    """Raised when a valid complete Summary Output cannot be produced."""


def normalize_output_language(value: str | None) -> str | None:
    """Validate a conservative BCP-47-style tag and normalize its casing."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_OUTPUT_LANGUAGE_LENGTH:
        raise ValueError("invalid output language tag")

    subtags = value.split("-")
    if not _LANGUAGE_SUBTAG.fullmatch(subtags[0]):
        raise ValueError("invalid output language tag")

    normalized = [subtags[0].lower()]
    index = 1
    if index < len(subtags) and _SCRIPT_SUBTAG.fullmatch(subtags[index]):
        normalized.append(subtags[index].title())
        index += 1
    if index < len(subtags) and _REGION_SUBTAG.fullmatch(subtags[index]):
        normalized.append(subtags[index].upper())
        index += 1

    variants: set[str] = set()
    for subtag in subtags[index:]:
        variant = subtag.lower()
        if not _VARIANT_SUBTAG.fullmatch(subtag) or variant in variants:
            raise ValueError("invalid output language tag")
        normalized.append(variant)
        variants.add(variant)
    return "-".join(normalized)


def _normalize_summary_language(value: str | None) -> str | None:
    try:
        return normalize_output_language(value)
    except ValueError as error:
        raise SummaryError("invalid output language tag") from error


def resolve_opencode_version(requested: str) -> str:
    """Use an exact version directly or resolve an opencode-ai npm dist-tag."""
    if _EXACT_VERSION.fullmatch(requested):
        return requested
    try:
        request = Request(
            "https://registry.npmjs.org/opencode-ai",
            headers={"Accept": "application/vnd.npm.install-v1+json"},
        )
        with urlopen(request, timeout=15) as response:
            metadata = json.load(response)
    except (OSError, URLError, TimeoutError) as error:
        raise SummaryError("could not query npm registry for opencode-ai") from error
    except (ValueError, UnicodeError) as error:
        raise SummaryError("invalid opencode-ai response from npm registry") from error

    if not isinstance(metadata, dict):
        raise SummaryError("invalid opencode-ai response from npm registry")
    tags = metadata.get("dist-tags")
    if not isinstance(tags, dict):
        raise SummaryError("invalid opencode-ai response from npm registry")
    if requested not in tags:
        raise SummaryError(
            f"invalid OpenCode version or unknown opencode-ai dist-tag {requested!r}"
        )
    resolved = tags[requested]
    if not isinstance(resolved, str) or _EXACT_VERSION.fullmatch(resolved) is None:
        raise SummaryError("invalid opencode-ai response from npm registry")
    return resolved


def _image_tag_for_version(version: str) -> str:
    # SemVer build metadata allows '+', which Docker image tags do not.
    tag = version.replace("+", "_")
    if len(tag) > _MAX_DOCKER_TAG_LENGTH:
        tag = "sha256-" + hashlib.sha256(version.encode("utf-8")).hexdigest()
    return f"tracebase-opencode:{tag}"


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
    debug_output: Path | None = None
    opencode_version: str = "latest"
    output_language: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "output_language", _normalize_summary_language(self.output_language)
        )


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
    """Read and validate the authoritative interval from Context index.json."""
    try:
        inventory = load_context_inventory(context)
    except (ContextInventoryError, OSError, UnicodeError) as error:
        raise SummaryError(
            "could not read requested interval from Context Output"
        ) from error
    try:
        request = ContextRequest.parse(*inventory.requested_interval)
    except (ContextInventoryError, ValueError) as error:
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
    expected_plan: tuple[PlannedShard, ...],
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
    final_errors = inspect_shards(work, context, expected_plan=expected_plan).errors
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
        for name in (
            "context-host",
            "context-evidence",
            "work",
            "runtime",
            "results",
        ):
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
    resolved_version = resolve_opencode_version(request.opencode_version)
    target.parent.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=".summary-run.", dir=target.parent))
    provenance: dict[str, Any] = {
        "format_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "model": request.model,
        "variant": request.variant,
        "output_language": request.output_language,
        "tracebase_version": _tracebase_version(),
    }
    try:
        context_host = run / "context-host"
        shutil.copytree(request.context, context_host)
        provenance["context_input"] = fingerprint_context(context_host)
        provenance["requested_interval"] = context_interval(context_host)
        context_evidence = run / "context-evidence"
        try:
            materialize_context_evidence(context_host, context_evidence)
        except (ContextInventoryError, OSError) as error:
            raise SummaryError("Context evidence materialization failed") from error
        provenance["model_context"] = {
            "mount": "/context:ro",
            "path": "context-evidence",
            "fingerprint": fingerprint_context(context_evidence),
        }
        work = run / "work"
        results = run / "results"
        runtime = run / "runtime"
        state = run / ".opencode-data"
        work.mkdir()
        results.mkdir()
        runtime.mkdir()
        try:
            inventory = load_context_inventory(context_host)
            mutable_state = load_mutable_state(context_host, inventory)
            reconciled_state = reconcile_mutable_state(mutable_state)
            (work / "MUTABLE_STATE.md").write_text(
                render_mutable_state(reconciled_state), encoding="utf-8"
            )
        except (
            ContextInventoryError,
            MutableStateError,
            OSError,
            UnicodeError,
        ) as error:
            raise SummaryError("Mutable state metadata validation failed") from error
        prompt = Path(__file__).parent / "prompts/summarizer-v1.md"
        task = work / "TASK.md"
        task.write_bytes(prompt.read_bytes())
        provenance["task_sha256"] = hashlib.sha256(task.read_bytes()).hexdigest()
        try:
            shard_plan = plan_shards(context_host)
            write_initial_plan(
                work,
                context_host,
                shard_plan,
                output_language=request.output_language,
            )
        except ValueError as error:
            raise SummaryError(
                f"Context inventory or shard planning failed: {error}"
            ) from None
        plan_errors = inspect_shard_plan(
            work, context_host, expected_plan=shard_plan
        ).errors
        if plan_errors:
            raise SummaryError(
                "Tracebase generated an invalid shard plan: " + "; ".join(plan_errors)
            )
        provenance["shard_plan"] = [
            {
                "id": shard.id,
                "items": list(shard.items),
                "readable_bytes": shard.readable_bytes,
                "task_sha256": hashlib.sha256(
                    (work / "shards" / shard.id / "TASK.md").read_bytes()
                ).hexdigest(),
            }
            for shard in shard_plan
        ]
        expected_plan = shard_plan
        dockerfile = Path(__file__).parent / "container/Dockerfile"
        if runner is None:
            config = prepare_state(state, request.model)
            image = _image_tag_for_version(resolved_version)
            try:
                ensure_image(image, dockerfile, resolved_version)
            except ContainerError as error:
                raise SummaryError(str(error)) from error
            runner = ContainerRunner(
                image,
                ContainerMounts(
                    context_evidence,
                    work,
                    results,
                    state,
                    task=task,
                    mutable_state=work / "MUTABLE_STATE.md",
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
            work, context_host, expected_plan=expected_plan
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
                context_host,
                expected_plan,
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
    opencode_version: str = "latest",
    output_language: str | None = None,
) -> Path:
    """Compose archive extraction with production summarization."""
    output_language = _normalize_summary_language(output_language)
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
                debug_output=debug_output,
                opencode_version=opencode_version,
                output_language=output_language,
            )
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
