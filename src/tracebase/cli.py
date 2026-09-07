"""The non-interactive Collection Run command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from .archive import Archive, ArchiveError, CollectionRange, CollectionRun
from .collector import CollectionContext, CollectionResult
from .github import collect as collect_github
from .github import resolve_context as resolve_github_context
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
    collect = commands.add_parser("collect", add_help=True, allow_abbrev=False)
    source_commands = collect.add_subparsers(dest="source", required=True)

    for source in ("github", "opencode"):
        source_parser = source_commands.add_parser(
            source, add_help=True, allow_abbrev=False
        )
        source_parser.add_argument("--archive", required=True)
        source_parser.add_argument("--from", dest="from_text", required=True)
        source_parser.add_argument("--to", dest="to_text", required=True)
        if source == "opencode":
            source_parser.add_argument("--instance-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Intentionally let collection/orchestration exceptions propagate.
    # Progress output provides context while the original traceback remains
    # visible for debugging; source content and credentials stay excluded.
    arguments = _parser().parse_args(argv)
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
