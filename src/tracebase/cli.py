"""The non-interactive Collection Run command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from .archive import Archive, ArchiveError, CollectionRange, CollectionRun
from .collector import CollectionResult
from .github import _actor, _GitHub
from .github import collect as collect_github
from .opencode import collect as collect_opencode
from .progress import (
    LineProgressReporter,
    ProgressEvent,
    ProgressReporter,
    RichProgressReporter,
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
    arguments = _parser().parse_args(argv)
    collection_range = CollectionRange.parse(arguments.from_text, arguments.to_text)
    archive = Archive(arguments.archive)
    run_id = archive.new_run_id()

    reporter = (
        RichProgressReporter(sys.stderr)
        if sys.stderr.isatty()
        else LineProgressReporter(sys.stderr)
    )
    with reporter:
        if arguments.source == "opencode":
            scope_id = arguments.instance_id
            run = CollectionRun(
                archive,
                "opencode",
                scope_id,
                collection_range,
                collector_version="0.1.0",
                effective_options={"instance_id": scope_id},
                run_id=run_id,
            )
            result = collect_opencode(run, reporter)
            published = _publish(run, result, reporter)
            print(
                f"collected run {run.run_id} with {result.snapshot_count} snapshots "
                f"at {archive.root / 'runs' / run.run_id}"
            )
            return 0

        # GitHub scope identity is its authenticated actor's stable node ID.
        scope_id, login = _actor(_GitHub())
        run = CollectionRun(
            archive,
            "github",
            scope_id,
            collection_range,
            collector_version="0.1.0",
            effective_options={"actor_login": login},
            run_id=run_id,
        )
        result = collect_github(run, (scope_id, login), reporter=reporter)
        published = _publish(run, result, reporter)
        print(f"{run.run_id}  {result.snapshot_count} snapshots  {published}")
        return 0
