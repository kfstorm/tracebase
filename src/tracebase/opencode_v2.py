"""Acquire the public V2 API's full, page-ordered responses."""

import json
from datetime import UTC, datetime
from typing import Any

from .archive import ArchiveError, CollectionRun, Snapshot
from .collector import CollectionResult
from .opencode_cli import HydrationProgress, run_opencode
from .progress import ProgressEvent, ProgressReporter

PAGE_LIMIT = 200
_MESSAGE_TYPES = frozenset(
    {
        "user",
        "assistant",
        "synthetic",
        "system",
        "skill",
        "shell",
        "compaction",
        "idle",
        "agent-switched",
        "model-switched",
        "location-switched",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def api(operation: str, parameters: dict[str, str]) -> tuple[bytes, dict[str, Any]]:
    # A private server avoids attaching to or changing a caller's background service.
    command = ["--standalone", "api", operation]
    for key, value in parameters.items():
        command.extend(["--param", f"{key}={value}"])
    raw = run_opencode(command)
    try:
        value = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ArchiveError("OpenCode V2 API response is invalid") from None
    if not isinstance(value, dict):
        raise ArchiveError("OpenCode V2 API response is invalid")
    return raw, value


def pages(
    operation: str, parameters: dict[str, str]
) -> list[tuple[bytes, dict[str, Any], dict[str, str]]]:
    result = []
    seen: set[str] = set()
    parameters = parameters | {"limit": str(PAGE_LIMIT), "order": "asc"}
    while True:
        raw, response = api(operation, parameters)
        data, cursor = response.get("data"), response.get("cursor")
        if not isinstance(data, list) or not isinstance(cursor, dict):
            raise ArchiveError("OpenCode V2 pagination response is invalid")
        if any(not isinstance(item, dict) for item in data):
            raise ArchiveError("OpenCode V2 API record is invalid")
        for field in ("next", "previous"):
            value = cursor.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                raise ArchiveError("OpenCode V2 pagination cursor is invalid")
        result.append((raw, response, parameters))
        next_cursor = cursor.get("next")
        if next_cursor is None:
            return result
        if not data or next_cursor in seen:
            raise ArchiveError("OpenCode V2 pagination is incomplete")
        seen.add(next_cursor)
        parameters = {key: value for key, value in parameters.items() if key != "order"}
        parameters = parameters | {"cursor": next_cursor}


def validate_records(
    responses: list[tuple[bytes, dict[str, Any], dict[str, str]]], *, messages: bool
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for _, response, _ in responses:
        for item in response["data"]:
            identifier, time = item.get("id"), item.get("time")
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in identifiers
            ):
                raise ArchiveError("OpenCode V2 record identity is invalid")
            identifiers.add(identifier)
            if not isinstance(time, dict):
                raise ArchiveError("OpenCode V2 record time is invalid")
            for field in ("created",) if messages else ("created", "updated"):
                stamp = time.get(field)
                if isinstance(stamp, bool) or not isinstance(stamp, int):
                    raise ArchiveError("OpenCode V2 record time is invalid")
                try:
                    datetime.fromtimestamp(stamp / 1000, UTC)
                except OverflowError, OSError, ValueError:
                    raise ArchiveError("OpenCode V2 record time is invalid") from None
            if messages:
                if item.get("type") not in _MESSAGE_TYPES:
                    raise ArchiveError("Unsupported OpenCode V2 message type")
                if item["type"] == "user" and not isinstance(item.get("text"), str):
                    raise ArchiveError("OpenCode V2 user message is invalid")
                if item["type"] == "assistant":
                    content = item.get("content")
                    if not isinstance(content, list) or any(
                        not isinstance(part, dict)
                        or part.get("type") not in {"text", "reasoning", "tool"}
                        or (
                            part["type"] == "text"
                            and not isinstance(part.get("text"), str)
                        )
                        for part in content
                    ):
                        raise ArchiveError("OpenCode V2 assistant message is invalid")
            else:
                location = item.get("location")
                if (
                    time["updated"] < time["created"]
                    or not isinstance(location, dict)
                    or not isinstance(location.get("directory"), str)
                    or not location["directory"]
                ):
                    raise ArchiveError("OpenCode V2 session is invalid")
            records.append(item)
    return records


def collect_v2(
    run: CollectionRun, reporter: ProgressReporter, version: str
) -> CollectionResult:
    started = _now()
    discovery = pages("session.list", {})
    sessions = validate_records(discovery, messages=False)
    selected = [
        session
        for session in sessions
        if run.collection_range.start.timestamp() * 1000
        <= session["time"]["updated"]
        < run.collection_range.end.timestamp() * 1000
    ]
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="opencode.discover",
            message=f"{len(selected)} sessions selected",
        )
    )
    hydration = HydrationProgress(reporter, len(selected))
    for completed, session in enumerate(selected, start=1):
        hydration.update(completed - 1, session["id"], "starting")
        message_pages = pages("session.message.list", {"sessionID": session["id"]})
        validate_records(message_pages, messages=True)
        evidence: dict[str, bytes] = {}
        requests = []
        for prefix, operation, responses in (
            ("sessions", "session.list", discovery),
            ("messages", "session.message.list", message_pages),
        ):
            for index, (raw, _, parameters) in enumerate(responses):
                path = f"{prefix}/{index:06d}.json"
                evidence[path] = raw
                requests.append(
                    {"path": path, "operation": operation, "parameters": parameters}
                )
        root = run.write_snapshot(
            Snapshot(
                source_kind="opencode",
                object_kind="session",
                source_id=session["id"],
                observation_window={"from": started, "to": _now()},
                evidence_files=tuple({"path": path} for path in evidence),
                metadata={
                    "protocol": "opencode-v2",
                    "opencode_version": version,
                    "source_instance_id": run.scope_id,
                    "collector_version": run.collector_version,
                    "effective_options": run.effective_options,
                    "requests": requests,
                },
            )
        )
        for path, raw in evidence.items():
            run.write_evidence(root, path, raw)
        hydration.update(completed, session["id"], "completed")
    hydration.finish()
    return CollectionResult(
        coverage={
            "protocol": "opencode-v2",
            "session_discovery_operation": "session.list",
            "session_discovery_page_count": len(discovery),
            "listed_session_count": len(sessions),
            "selected_session_count": len(selected),
            "observation_window": {"from": started, "to": _now()},
        }
    )
