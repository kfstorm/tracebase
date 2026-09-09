"""GitHub whitelist projection rendering as deterministic Markdown."""

from __future__ import annotations

import json
from typing import Any

from .context import ContextExtractionResult, ContextItem
from .context_render import fenced, item_front_matter, render_provenance


def _record_value(record: dict[str, Any]) -> dict[str, Any] | str | None:
    representations = record.get("representations")
    if not isinstance(representations, list) or not representations:
        return None
    value = representations[-1].get("value")
    return value if isinstance(value, (dict, str)) else None


def _github_observation_label(representation: dict[str, Any]) -> str:
    observation = representation.get("observation")
    label = (
        f"Observation {observation}"
        if isinstance(observation, int) and observation > 0
        else "Observation unknown"
    )
    window = representation.get("observation_window")
    if isinstance(window, dict):
        start = window.get("from")
        end = window.get("to")
        if isinstance(start, str) and isinstance(end, str):
            return f"{label} [{start}, {end})"
    return label


def _field_versions(
    record: dict[str, Any], key: str
) -> list[tuple[Any, tuple[str, ...]]]:
    versions: list[tuple[Any, list[str], str]] = []
    representations = record.get("representations")
    if not isinstance(representations, list):
        return []
    for representation in representations:
        if not isinstance(representation, dict):
            continue
        value = representation.get("value")
        if not isinstance(value, dict) or key not in value:
            continue
        field_value = value[key]
        marker = json.dumps(field_value, ensure_ascii=False, sort_keys=True)
        label = _github_observation_label(representation)
        for _version_value, labels, version_marker in versions:
            if version_marker == marker:
                labels.append(label)
                break
        else:
            versions.append((field_value, [label], marker))
    return [(value, tuple(labels)) for value, labels, _ in versions]


def _github_actor(value: dict[str, Any]) -> str | None:
    for key in ("user", "actor", "author"):
        actor = value.get(key)
        if isinstance(actor, dict):
            login = actor.get("login")
            if isinstance(login, str):
                return login
    return None


def _github_times(value: dict[str, Any]) -> list[str]:
    return [
        value[key]
        for key in (
            "created_at",
            "updated_at",
            "submitted_at",
            "merged_at",
            "closed_at",
        )
        if isinstance(value.get(key), str)
    ]


def _github_observations(record: dict[str, Any]) -> str:
    representations = record.get("representations")
    if not isinstance(representations, list):
        return ""
    labels = []
    for representation in representations:
        if isinstance(representation, dict):
            label = _github_observation_label(representation)
            if label not in labels:
                labels.append(label)
    return ", ".join(label[0].lower() + label[1:] for label in labels)


def _render_scalar_versions(
    lines: list[str], label: str, versions: list[tuple[Any, tuple[str, ...]]]
) -> None:
    if len(versions) <= 1:
        return
    lines.extend([f"- Observed {label} versions:"])
    for value, observations in versions:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"  - {', '.join(observations)}: `{rendered}`")


def _render_body(lines: list[str], record: dict[str, Any]) -> None:
    versions = _field_versions(record, "body")
    if not versions:
        return
    latest = _record_value(record)
    current = latest.get("body") if isinstance(latest, dict) else None
    if not isinstance(current, str):
        return
    lines.extend(["", "### Body", "", "Current observed value:", ""])
    lines.extend(fenced(current, "text"))
    if len(versions) > 1:
        lines.extend(["", "#### Observed versions", ""])
        for value, observations in versions:
            if not isinstance(value, str):
                continue
            lines.append(f"- {', '.join(observations)}:")
            lines.extend(f"  {line}" for line in fenced(value, "text"))
    lines.append("")


def _render_github_record(
    lines: list[str],
    record: dict[str, Any],
    lifecycle_record: dict[str, Any] | None = None,
) -> None:
    kind = record.get("kind")
    value = _record_value(record)
    if kind == "aggregate-diff":
        if isinstance(value, str):
            lines.extend(
                [
                    "## Aggregate Diff",
                    "",
                    "This diff does not establish a fix or fixing commit.",
                    "",
                ]
            )
            lines.extend(fenced(value, "diff"))
            lines.append("")
        return
    if not isinstance(value, dict):
        return
    if kind not in {
        "issue",
        "pull-request",
        "ordinary-comment",
        "review",
        "inline-comment",
        "review-thread",
        "timeline",
    }:
        return
    if kind == "timeline" and value.get("event") not in {
        "closed",
        "reopened",
        "merged",
        "labeled",
        "unlabeled",
        "locked",
        "unlocked",
        "ready_for_review",
        "converted_to_draft",
    }:
        return
    heading = {
        "issue": "Issue",
        "pull-request": "Pull Request",
        "ordinary-comment": "Comment",
        "review": "Review",
        "inline-comment": "Inline review comment",
        "review-thread": "Review thread",
        "timeline": "Lifecycle event",
    }[kind]
    lines.extend([f"## {heading} - `{record['native_id']}`", ""])
    if kind in {"issue", "pull-request"}:
        for label, key in (
            ("Title", "title"),
            ("State", "state"),
            ("Number", "number"),
        ):
            if isinstance(value.get(key), (str, int)):
                lines.append(f"- {label}: `{value[key]}`")
            _render_scalar_versions(lines, label, _field_versions(record, key))
        if kind == "pull-request" and lifecycle_record is not None:
            lifecycle = _record_value(lifecycle_record)
            if isinstance(lifecycle, dict):
                for label, key in (("Merged", "merged"), ("Draft", "draft")):
                    if isinstance(lifecycle.get(key), bool):
                        lines.append(
                            f"- {label}: `{'true' if lifecycle[key] else 'false'}`"
                        )
                        _render_scalar_versions(
                            lines, label, _field_versions(lifecycle_record, key)
                        )
                if isinstance(lifecycle.get("merged_at"), str):
                    lines.append(f"- Merged at: {lifecycle['merged_at']}")
                    _render_scalar_versions(
                        lines,
                        "Merged at",
                        _field_versions(lifecycle_record, "merged_at"),
                    )
    actor = _github_actor(value)
    if actor is not None:
        lines.append(f"- Actor: `{actor}`")
    lines.extend(f"- Timestamp: {timestamp}" for timestamp in _github_times(value))
    _render_body(lines, record)
    if kind == "review" and isinstance(value.get("state"), str):
        lines.append(f"- Review state: `{value['state']}`")
    if kind == "inline-comment":
        location = {
            key: value[key]
            for key in ("path", "line", "side", "start_line", "start_side")
            if isinstance(value.get(key), (str, int))
        }
        if location:
            lines.append(f"- Location: `{json.dumps(location, sort_keys=True)}`")
    if kind == "review-thread":
        for key in ("isResolved", "isOutdated"):
            if isinstance(value.get(key), bool):
                lines.append(f"- Current {key}: `{str(value[key]).lower()}`")
                _render_scalar_versions(lines, key, _field_versions(record, key))
    observations = _github_observations(record)
    if observations:
        lines.append(f"- Observed in: {observations}")
    lines.append("")


def render_github(item: ContextItem, result: ContextExtractionResult) -> list[str]:
    assert item.github is not None
    projection = item.github
    lines = item_front_matter(item, result, "github", projection)
    lines.extend(
        [
            "# GitHub Item",
            "",
            "Source-native thread state is current observed state, not proof of a fix.",
            "",
        ]
    )
    render_provenance(lines, item)
    lifecycle_records = {
        (record.get("kind"), record.get("native_id")): record
        for record in projection.records
        if record.get("kind") == "pull-request-payload"
    }
    for record in projection.records:
        if record.get("kind") == "pull-request-payload":
            continue
        lifecycle_record = lifecycle_records.get(
            ("pull-request-payload", record.get("native_id"))
        )
        _render_github_record(lines, record, lifecycle_record)
    if projection.relations:
        lines.extend(["## Within-Item Structure", ""])
        for relation in projection.relations:
            lines.append(
                f"- `{relation['kind']}`: "
                f"`{relation['from_native_id']}` -> `{relation['to_native_id']}`"
            )
        lines.append("")
    return lines
