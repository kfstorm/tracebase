"""The non-interactive Collection Run command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .archive import Archive, ArchiveError, CollectionRange, CollectionRun


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArchiveError("invalid command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="tracebase", add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", add_help=True)
    source_commands = collect.add_subparsers(dest="source", required=True)

    for source in ("github", "opencode"):
        source_parser = source_commands.add_parser(source, add_help=True)
        source_parser.add_argument("--archive", required=True)
        source_parser.add_argument("--from", dest="from_text", required=True)
        source_parser.add_argument("--to", dest="to_text", required=True)
        if source == "opencode":
            source_parser.add_argument("--instance-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        collection_range = CollectionRange.parse(arguments.from_text, arguments.to_text)
        archive = Archive(arguments.archive)
        run_id = archive.new_run_id()

        if arguments.source == "opencode":
            scope_id = arguments.instance_id
            if archive.has_overlap("opencode", scope_id, collection_range):
                raise ArchiveError("collection range overlaps a published run")
            CollectionRun(
                archive,
                "opencode",
                scope_id,
                collection_range,
                collector_version="0",
                effective_options={"instance_id": scope_id},
                run_id=run_id,
            )
        else:
            # GitHub scope identity is obtained by its future collector, not this fail-closed path.
            archive.create_staging(run_id)
        raise ArchiveError(f"{arguments.source} collector is not implemented")
    except ArchiveError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError, RuntimeError:
        print("error: unable to prepare Collection Run", file=sys.stderr)
        return 1
