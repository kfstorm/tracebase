"""Validation and observability for the root Summarizer's shard protocol."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .attribution import AttributionMode

_STATUS_BLOCK = re.compile(
    r"<!--\s*SHARD_STATUS_BEGIN\s*-->(.*?)<!--\s*SHARD_STATUS_END\s*-->",
    re.DOTALL,
)
_SHARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_ATTRIBUTION_MODE_VALUES = {mode.value for mode in AttributionMode}
_ATTRIBUTION_MODE = re.compile(r"attribution mode: `([^`]+)`")
_CONTEXT_LINK = re.compile(r"\]\(([^)]+)\)")
_GITHUB_SCOPE = re.compile(r"(?:GitHub )?repository (?P<repo>[^,;:]+)")
_OPENCODE_SCOPE = re.compile(r"OpenCode(?: project)? (?P<project>[^:;]+)")
_BRACE_SCOPE = re.compile(r"(?P<prefix>[^{}]+?)/\{(?P<items>[^{}]+)\}\Z")
_GITHUB_REPOSITORY_SEPARATOR_COUNT = 2


@dataclass(frozen=True, slots=True)
class ShardObservability:
    """Terminal shard counts and protocol errors recorded by the harness."""

    errors: tuple[str, ...]


def _status_data(notes: str) -> Any:
    match = _STATUS_BLOCK.search(notes)
    if match is None:
        raise ValueError("NOTES.md has no SHARD_STATUS block")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("NOTES.md SHARD_STATUS block is not valid JSON") from error


def _context_items(index_lines: tuple[str, ...]) -> dict[str, str]:
    """Return projected item roots and their declared attribution modes."""
    items: dict[str, str] = {}
    for line in index_lines:
        mode = _ATTRIBUTION_MODE.search(line)
        if mode is None:
            continue
        for target in _CONTEXT_LINK.findall(line):
            if not target.endswith("/overview.md"):
                continue
            root = target.removesuffix("/overview.md").removeprefix("./")
            items[root] = mode.group(1)
    return items


def _scope_parts(scope: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(scope):
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(depth - 1, 0)
        elif character == "," and depth == 0:
            parts.append(scope[start:index].strip())
            start = index + 1
    parts.append(scope[start:].strip())
    return tuple(part for part in parts if part)


def _source_scope_selectors(scope: str) -> tuple[tuple[str, bool], ...]:
    selectors: list[tuple[str, bool]] = []
    for part in _scope_parts(scope.removeprefix("misc:").strip()):
        match = _BRACE_SCOPE.fullmatch(part)
        if match is not None:
            prefix = match.group("prefix").rstrip("/")
            selectors.extend(
                (f"{prefix}/{item.strip()}", False)
                for item in match.group("items").split(",")
                if item.strip()
            )
            continue
        path = part.rstrip("/")
        is_broad = (
            path.startswith("github/")
            and path.count("/") == _GITHUB_REPOSITORY_SEPARATOR_COUNT
        ) or (path.startswith("opencode/") and "/session/" not in path)
        selectors.append((path, is_broad))
    return tuple(selectors)


def _scope_selectors(scope: str) -> tuple[tuple[str, bool], ...]:
    cleaned = scope.split(" (", 1)[0].strip()
    if cleaned.startswith("/context/"):
        selector = cleaned.removeprefix("/context/").rstrip("/")
        return (
            (selector, not (selector == "chatgpt" or selector.startswith("chatgpt/"))),
        )
    if cleaned.startswith(("github/", "opencode/", "chatgpt/", "misc:")):
        return _source_scope_selectors(cleaned)
    github_scope = _GITHUB_SCOPE.match(cleaned)
    if github_scope is not None:
        return ((f"github/{github_scope.group('repo').strip()}", True),)
    opencode_scope = _OPENCODE_SCOPE.match(cleaned)
    if opencode_scope is not None:
        return ((f"opencode{opencode_scope.group('project').strip()}", True),)
    selectors: list[tuple[str, bool]] = []
    for part in _scope_parts(cleaned):
        selectors.extend(_source_scope_selectors(part))
    return tuple(selectors)


def _scope_items(scope: str, item_roots: set[str]) -> frozenset[str]:
    selectors = _scope_selectors(scope)
    return frozenset(
        root
        for root in item_roots
        if any(
            root == selector or (broad and root.startswith(f"{selector}/"))
            for selector, broad in selectors
        )
    )


def _normalized_headings(report: str) -> set[str]:
    headings: set[str] = set()
    fenced = False
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING.match(line)
        if match is None:
            continue
        heading = re.sub(r"\s+#+\s*$", "", match.group(1)).strip()
        headings.add(re.sub(r"\s+", " ", heading).casefold())
    return headings


def inspect_shards(
    work_dir: Path, context_dir: Path | None = None
) -> ShardObservability:
    """Check every declared shard has one non-empty canonical report."""
    notes_path = work_dir / "NOTES.md"
    try:
        notes = notes_path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return ShardObservability((f"could not read {notes_path}",))

    try:
        data = _status_data(notes)
    except ValueError as error:
        return ShardObservability((str(error),))
    if not isinstance(data, dict) or not isinstance(data.get("shards"), list):
        return ShardObservability(("SHARD_STATUS.shards must be a list",))
    if context_dir is not None and not data["shards"]:
        try:
            has_context = any(path.is_file() for path in context_dir.rglob("*"))
        except OSError:
            has_context = True
        if has_context:
            return ShardObservability(("non-empty Context has no declared shards",))

    errors: list[str] = []
    index_lines: tuple[str, ...] = ()
    index_error = False
    if context_dir is not None:
        try:
            index_lines = tuple(
                (context_dir / "index.md").read_text(encoding="utf-8").splitlines()
            )
        except OSError, UnicodeError:
            index_error = True
            errors.append("could not read Context index")
    context_items = _context_items(index_lines)
    item_roots = set(context_items)
    shard_items: list[tuple[str, frozenset[str]]] = []
    seen: set[str] = set()
    for item in data["shards"]:
        if not isinstance(item, dict):
            errors.append("each shard status must be an object")
            continue
        shard_id = item.get("id")
        status = item.get("status")
        retry_count = item.get("retry_count", 0)
        report = item.get("report")
        attribution_modes = item.get("attribution_modes")
        if not isinstance(shard_id, str) or _SHARD_ID.fullmatch(shard_id) is None:
            errors.append("shard id is invalid")
            continue
        if shard_id in seen:
            errors.append(f"shard {shard_id!r} is declared more than once")
            continue
        seen.add(shard_id)
        if not isinstance(retry_count, int) or isinstance(retry_count, bool):
            errors.append(f"shard {shard_id!r} retry_count is invalid")
        elif retry_count < 0 or retry_count > 1:
            errors.append(f"shard {shard_id!r} was retried more than once")
        if status == "failed":
            errors.append(f"shard {shard_id!r} reported failure")
        elif status != "complete":
            errors.append(f"shard {shard_id!r} is not in a terminal state")
        if (
            not isinstance(attribution_modes, list)
            or not attribution_modes
            or not all(
                isinstance(mode, str) and mode in _ATTRIBUTION_MODE_VALUES
                for mode in attribution_modes
            )
        ):
            errors.append(f"shard {shard_id!r} attribution modes are invalid")
        scope = item.get("scope")
        if context_dir is not None and not index_error:
            if not isinstance(scope, str) or not scope:
                errors.append(f"shard {shard_id!r} scope is invalid")
            elif not isinstance(attribution_modes, list):
                pass
            else:
                resolved_items = _scope_items(scope, item_roots)
                shard_items.append((shard_id, resolved_items))
                expected_modes = {context_items[root] for root in resolved_items}
                if not resolved_items:
                    errors.append(
                        f"shard {shard_id!r} scope does not match Context items"
                    )
                elif expected_modes != set(attribution_modes):
                    errors.append(
                        f"shard {shard_id!r} attribution modes do not match Context"
                    )

        expected_report = f"/work/shards/{shard_id}.md"
        if report != expected_report:
            errors.append(f"shard {shard_id!r} has a non-canonical report path")
        else:
            report_path = work_dir / "shards" / f"{shard_id}.md"
            if report_path.is_symlink() or not report_path.is_file():
                errors.append(f"shard {shard_id!r} canonical report is missing")
            else:
                try:
                    report_text = report_path.read_text(encoding="utf-8")
                    if not report_text.strip():
                        errors.append(f"shard {shard_id!r} canonical report is empty")
                    elif not {
                        "user work",
                        "context-only evidence",
                    }.issubset(_normalized_headings(report_text)):
                        errors.append(
                            f"shard {shard_id!r} report does not separate user work "
                            "from context-only evidence"
                        )
                except OSError, UnicodeError:
                    errors.append(f"shard {shard_id!r} canonical report cannot be read")
    if context_dir is not None and not index_error:
        memberships: dict[str, list[str]] = {root: [] for root in item_roots}
        for shard_id, resolved_items in shard_items:
            for root in resolved_items:
                memberships[root].append(shard_id)
        for root, owners in sorted(memberships.items()):
            if not owners:
                errors.append(f"Context item {root!r} is not covered by any shard")
            elif len(owners) > 1:
                errors.append(
                    f"Context item {root!r} is covered by multiple shards: "
                    + ", ".join(owners)
                )
    return ShardObservability(tuple(errors))
