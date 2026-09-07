"""Collect OpenCode sessions through its supported command-line interface."""

from __future__ import annotations

import json
import os
import re
import secrets
import select
import subprocess
import time
from base64 import b64encode
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .archive import ArchiveError, CollectionRun, Snapshot
from .collector import CollectionContext, CollectionResult
from .progress import ProgressEvent, ProgressReporter

_SERVER_URL_PATTERN = re.compile(r"http://127\.0\.0\.1:\d+")
_SERVER_START_TIMEOUT_SECONDS = 5
_DISCOVERY_LIMIT = 10_000
_SERVER_USERNAME = "tracebase"


def _observation_time() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run_opencode(arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["opencode", *arguments], capture_output=True, check=False
        )
    except OSError:
        raise ArchiveError("OpenCode CLI is unavailable") from None
    if result.returncode != 0:
        raise ArchiveError("OpenCode CLI operation failed")
    return result.stdout


def _start_server() -> tuple[subprocess.Popen[str], str, str]:
    password = secrets.token_urlsafe()
    environment = os.environ | {
        "OPENCODE_SERVER_USERNAME": _SERVER_USERNAME,
        "OPENCODE_SERVER_PASSWORD": password,
    }
    try:
        process = subprocess.Popen(
            [
                "opencode",
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                "--log-level",
                "ERROR",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )
    except OSError:
        raise ArchiveError("OpenCode server is unavailable") from None
    if process.stdout is None:
        process.terminate()
        raise ArchiveError("OpenCode server is unavailable")
    deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        readable, _, _ = select.select(
            [process.stdout], [], [], deadline - time.monotonic()
        )
        if not readable:
            break
        match = _SERVER_URL_PATTERN.search(process.stdout.readline())
        if match:
            return process, match.group(), password
        if process.poll() is not None:
            break
    _stop_server(process)
    raise ArchiveError("OpenCode server did not start")


def _stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_SERVER_START_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _discover_sessions(
    server_url: str, password: str, run: CollectionRun
) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "start": int(run.collection_range.start.timestamp() * 1000),
            "archived": "true",
            "limit": _DISCOVERY_LIMIT,
        }
    )
    credentials = b64encode(f"{_SERVER_USERNAME}:{password}".encode()).decode()
    request = Request(
        f"{server_url}/experimental/session?{query}",
        headers={"Authorization": f"Basic {credentials}"},
    )
    try:
        with urlopen(request, timeout=_SERVER_START_TIMEOUT_SECONDS) as response:
            content = response.read()
            next_cursor = response.headers.get("x-next-cursor")
    except HTTPError, URLError, OSError:
        raise ArchiveError("OpenCode session discovery failed") from None
    if next_cursor is not None:
        raise ArchiveError("OpenCode session discovery is incomplete")
    return _parse_sessions(content)


def _parse_sessions(content: bytes) -> list[dict[str, Any]]:
    try:
        sessions = json.loads(content)
    except json.JSONDecodeError:
        raise ArchiveError("OpenCode session list is invalid") from None
    if not isinstance(sessions, list):
        raise ArchiveError("OpenCode session list is invalid")
    for session in sessions:
        if not isinstance(session, dict):
            raise ArchiveError("OpenCode session list is invalid")
        if not isinstance(session.get("id"), str) or not session["id"]:
            raise ArchiveError("OpenCode session list is invalid")
        time_data = session.get("time")
        if not isinstance(time_data, dict):
            raise ArchiveError("OpenCode session list is invalid")
        for field in ("created", "updated"):
            if isinstance(time_data.get(field), bool) or not isinstance(
                time_data.get(field), int
            ):
                raise ArchiveError("OpenCode session list is invalid")
    return sessions


def _session_interval(session: dict[str, Any]) -> tuple[datetime, datetime]:
    try:
        time_data = session["time"]
        created = datetime.fromtimestamp(time_data["created"] / 1000, UTC)
        updated = datetime.fromtimestamp(time_data["updated"] / 1000, UTC)
    except OverflowError, OSError, TypeError, ValueError:
        raise ArchiveError("OpenCode session timestamps are invalid") from None
    if updated < created:
        raise ArchiveError("OpenCode session timestamps are invalid")
    return created, updated


def resolve_context(instance_id: str) -> CollectionContext:
    """Resolve the caller-provided OpenCode source instance."""

    return CollectionContext(
        source_kind="opencode",
        scope_id=instance_id,
        collector_version="0.1.0",
        effective_options={"instance_id": instance_id},
    )


def collect(
    run: CollectionRun,
    reporter: ProgressReporter,
) -> CollectionResult:
    """Export every session whose lifecycle intersects the Collection Range."""

    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="opencode.discover",
            label="Discovering OpenCode sessions",
        )
    )
    opencode_version = _run_opencode(["--version"]).decode("utf-8", "replace").strip()
    list_started_at = _observation_time()
    server, server_url, password = _start_server()
    try:
        sessions = _discover_sessions(server_url, password, run)
    finally:
        _stop_server(server)
    list_completed_at = _observation_time()
    selected: list[dict[str, Any]] = []
    for session in sessions:
        created, updated = _session_interval(session)
        if created < run.collection_range.end and run.collection_range.start <= updated:
            selected.append(session)

    reporter.emit(
        ProgressEvent(
            kind="update",
            task_id="opencode.discover",
            completed=len(sessions),
            message=f"{len(sessions)} sessions listed, {len(selected)} selected",
        )
    )
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="opencode.discover",
            message=f"{len(selected)} sessions selected from {len(sessions)}",
        )
    )
    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="opencode.hydrate",
            label="Hydrating OpenCode sessions",
            total=len(selected),
        )
    )
    for completed, session in enumerate(selected, start=1):
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="opencode.hydrate",
                completed=completed - 1,
                total=len(selected),
                current=session["id"],
            )
        )
        observation_started_at = _observation_time()
        export_command = ["opencode", "export", session["id"]]
        export = _run_opencode(export_command[1:])
        observation_completed_at = _observation_time()
        snapshot_root = run.write_snapshot(
            Snapshot(
                source_kind="opencode",
                object_kind="session",
                source_id=session["id"],
                observation_window={
                    "from": observation_started_at,
                    "to": observation_completed_at,
                },
                evidence_files=({"path": "session.json"},),
                metadata={
                    "export_command": export_command,
                    "collector_version": run.collector_version,
                    "opencode_version": opencode_version,
                    "effective_options": run.effective_options,
                    "source_instance_id": run.scope_id,
                    "session": session,
                },
            )
        )
        run.write_evidence(snapshot_root, "session.json", export)
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="opencode.hydrate",
                completed=completed,
                total=len(selected),
                current=session["id"],
            )
        )

    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="opencode.hydrate",
            completed=len(selected),
            message=f"{len(selected)} sessions",
        )
    )
    return CollectionResult(
        coverage={
            "session_discovery_endpoint": "/experimental/session",
            "session_discovery_options": {
                "start": int(run.collection_range.start.timestamp() * 1000),
                "archived": True,
                "limit": _DISCOVERY_LIMIT,
            },
            "observation_window": {"from": list_started_at, "to": list_completed_at},
            "listed_session_count": len(sessions),
            "selected_session_count": len(selected),
        }
    )
