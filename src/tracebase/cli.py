"""The non-interactive Collection Run command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from .archive import Archive, ArchiveError, CollectionRange, CollectionRun
from .chatgpt import authenticate as authenticate_chatgpt
from .chatgpt import collect as collect_chatgpt
from .chatgpt import reset as reset_chatgpt
from .chatgpt import resolve_context as resolve_chatgpt_context
from .chatgpt import status as status_chatgpt
from .collector import CollectionContext, CollectionResult
from .context import ContextError, ContextRequest, generate_context
from .github import collect as collect_github
from .github import resolve_context as resolve_github_context
from .github import sync_identity as sync_github_identity
from .opencode import (
    collect as collect_opencode,
)
from .opencode import (
    resolve_context as resolve_opencode_context,
)
from .progress import (
    LineProgressSink,
    ProgressEvent,
    ProgressReporter,
    RichProgressSink,
)
from .summary import SummaryError, SummaryRequest, summarize, summarize_archive


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ArchiveError("invalid command arguments")


def _publish(
    run: CollectionRun, result: CollectionResult, reporter: ProgressReporter
) -> Path:
    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="publish",
            label="Publishing archive",
        )
    )
    published = run.publish(result.coverage)
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="publish",
            message="Archive published",
        )
    )
    return published


def _new_run(
    archive: Archive,
    collection_range: CollectionRange,
    run_id: str,
    context: CollectionContext,
) -> CollectionRun:
    return CollectionRun(
        archive,
        context.source_kind,
        context.scope_id,
        collection_range,
        collector_version=context.collector_version,
        effective_options=context.effective_options,
        run_id=run_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="tracebase", add_help=True, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", add_help=True, allow_abbrev=False)
    auth_sources = auth.add_subparsers(dest="source", required=True)
    chatgpt_auth = auth_sources.add_parser("chatgpt", add_help=True, allow_abbrev=False)
    chatgpt_actions = chatgpt_auth.add_subparsers(dest="auth_action")
    chatgpt_actions.add_parser("status", add_help=True, allow_abbrev=False)
    chatgpt_actions.add_parser("reset", add_help=True, allow_abbrev=False)
    collect = commands.add_parser("collect", add_help=True, allow_abbrev=False)
    source_commands = collect.add_subparsers(dest="source", required=True)

    for source in ("chatgpt", "github", "opencode"):
        source_parser = source_commands.add_parser(
            source, add_help=True, allow_abbrev=False
        )
        source_parser.add_argument("--archive", required=True)
        source_parser.add_argument("--from", dest="from_text", required=True)
        source_parser.add_argument("--to", dest="to_text", required=True)
        if source == "chatgpt":
            source_parser.add_argument(
                "--browser-mode",
                choices=("headless", "headed"),
                default="headless",
            )
        if source == "opencode":
            source_parser.add_argument("--instance-id", required=True)
    context = commands.add_parser("context", add_help=True, allow_abbrev=False)
    context.add_argument("--archive", required=True)
    context.add_argument("--from", dest="from_text", required=True)
    context.add_argument("--to", dest="to_text", required=True)
    context.add_argument("--output", required=True)
    summary = commands.add_parser("summary", add_help=True, allow_abbrev=False)
    inputs = summary.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--archive")
    inputs.add_argument("--context")
    summary.add_argument("--from", dest="from_text")
    summary.add_argument("--to", dest="to_text")
    summary.add_argument("--model", required=True)
    summary.add_argument("--variant")
    summary.add_argument(
        "--opencode-version", default="latest", metavar="VERSION_OR_TAG"
    )
    summary.add_argument("--output", required=True)
    summary.add_argument("--context-output")
    summary.add_argument("--debug-output")
    identity = commands.add_parser("identity", add_help=True, allow_abbrev=False)
    identity_sources = identity.add_subparsers(dest="identity_source", required=True)
    github_identity = identity_sources.add_parser(
        "github", add_help=True, allow_abbrev=False
    )
    identity_actions = github_identity.add_subparsers(
        dest="identity_action", required=True
    )
    sync_identity = identity_actions.add_parser(
        "sync", add_help=True, allow_abbrev=False
    )
    sync_identity.add_argument("--archive", required=True)
    return parser


def _sync_github_identity(archive: str) -> int:
    try:
        profile = sync_github_identity(archive)
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"GitHub identity profile refreshed at {profile}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # Intentionally let collection/orchestration exceptions propagate.
    # Progress output provides context while the original traceback remains
    # visible for debugging; Context Output itself is not a redaction boundary.
    try:
        arguments = _parser().parse_args(argv)
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1
    if arguments.command == "auth":
        if arguments.source != "chatgpt":
            raise AssertionError("unsupported authentication source")
        try:
            if arguments.auth_action == "status":
                session = status_chatgpt()
                if session is None:
                    print("Not authenticated")
                    return 1
                print(f"Authenticated as {session.label}")
                return 0
            if arguments.auth_action == "reset":
                reset_chatgpt()
                print("ChatGPT browser profile reset")
                return 0
            authenticate_chatgpt()
        except ArchiveError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if arguments.command == "identity":
        return _sync_github_identity(arguments.archive)
    if arguments.command == "context":
        try:
            request = ContextRequest.parse(arguments.from_text, arguments.to_text)
            published = generate_context(arguments.archive, request, arguments.output)
        except ContextError as error:
            print(str(error), file=sys.stderr)
            return 1
        except OSError, TypeError, ValueError, RuntimeError:
            print("context operation failed", file=sys.stderr)
            return 1
        print(f"context output published at {published}")
        return 0
    if arguments.command == "summary":
        try:
            if arguments.archive is not None:
                if not arguments.from_text or not arguments.to_text:
                    raise SummaryError("archive summary requires --from and --to")
                request = ContextRequest.parse(arguments.from_text, arguments.to_text)
                published = summarize_archive(
                    Path(arguments.archive),
                    Path(arguments.context_output)
                    if arguments.context_output
                    else None,
                    request,
                    arguments.model,
                    arguments.variant,
                    Path(arguments.output),
                    Path(arguments.debug_output) if arguments.debug_output else None,
                    opencode_version=arguments.opencode_version,
                )
            else:
                if arguments.from_text or arguments.to_text or arguments.context_output:
                    raise SummaryError(
                        "--from, --to, and --context-output require --archive"
                    )
                published = summarize(
                    SummaryRequest(
                        Path(arguments.context),
                        arguments.model,
                        arguments.variant,
                        Path(arguments.output),
                        debug_output=Path(arguments.debug_output)
                        if arguments.debug_output
                        else None,
                        opencode_version=arguments.opencode_version,
                    )
                )
        except (ContextError, SummaryError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(f"summary output published at {published}")
        return 0
    collection_range = CollectionRange.parse(arguments.from_text, arguments.to_text)
    archive = Archive(arguments.archive)
    run_id = archive.new_run_id()

    sink = (
        RichProgressSink(sys.stderr)
        if sys.stderr.isatty()
        else LineProgressSink(sys.stderr)
    )
    with sink:
        progress = ProgressReporter(sink)
        context: CollectionContext
        if arguments.source == "chatgpt":
            try:
                progress.emit(
                    ProgressEvent(
                        kind="start",
                        task_id="prepare",
                        label="Preparing ChatGPT collection",
                    )
                )
                context = resolve_chatgpt_context(arguments.browser_mode)
                run = _new_run(archive, collection_range, run_id, context)
                progress.emit(ProgressEvent(kind="finish", task_id="prepare"))
                result = collect_chatgpt(run, progress, arguments.browser_mode)
                published = _publish(run, result, progress)
            except ArchiveError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(
                f"collected run {run.run_id} with {run.snapshot_count} snapshots "
                f"at {archive.root / 'runs' / run.run_id}"
            )
            return 0

        if arguments.source == "opencode":
            progress.emit(
                ProgressEvent(
                    kind="start",
                    task_id="prepare",
                    label="Preparing OpenCode collection",
                )
            )
            context = resolve_opencode_context(arguments.instance_id)
            run = _new_run(archive, collection_range, run_id, context)
            progress.emit(ProgressEvent(kind="finish", task_id="prepare"))
            result = collect_opencode(run, progress)
            published = _publish(run, result, progress)
            print(
                f"collected run {run.run_id} with {run.snapshot_count} snapshots "
                f"at {archive.root / 'runs' / run.run_id}"
            )
            return 0

        progress.emit(
            ProgressEvent(
                kind="start",
                task_id="prepare",
                label="Preparing GitHub collection",
            )
        )
        context = resolve_github_context()
        run = _new_run(archive, collection_range, run_id, context)
        progress.emit(ProgressEvent(kind="finish", task_id="prepare"))
        result = collect_github(run, progress)
        published = _publish(run, result, progress)
        print(f"{run.run_id}  {run.snapshot_count} snapshots  {published}")
        return 0
