"""Collect OpenCode sessions through its supported command-line interface."""

from __future__ import annotations

import json
import os
import re
import secrets
import select
import subprocess
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .archive import ArchiveError, CollectionRun, Snapshot

_SERVER_URL_PATTERN = re.compile(r"http://127\.0\.0\.1:\d+")
_SERVER_START_TIMEOUT_SECONDS = 5
_DISCOVERY_LIMIT = 10_000


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


def _start_server() -> tuple[subprocess.Popen[str], str]:
    environment = os.environ | {"OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe()}
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
            return process, match.group()
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


def _discover_sessions(server_url: str, run: CollectionRun) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "start": int(run.collection_range.start.timestamp() * 1000),
            "archived": "true",
            "limit": _DISCOVERY_LIMIT,
        }
    )
    try:
        with urlopen(
            f"{server_url}/experimental/session?{query}",
            timeout=_SERVER_START_TIMEOUT_SECONDS,
        ) as response:
            content = response.read()
            next_cursor = response.headers.get("x-next-cursor")
    except HTTPError, URLError, OSError:
        raise ArchiveError("OpenCode session discovery failed") from None
    if next_cursor:
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
        for field in ("created", "updated"):
            if isinstance(session.get(field), bool) or not isinstance(
                session.get(field), int
            ):
                raise ArchiveError("OpenCode session list is invalid")
    return sessions


def _session_interval(session: dict[str, Any]) -> tuple[datetime, datetime]:
    try:
        created = datetime.fromtimestamp(session["created"] / 1000, UTC)
        updated = datetime.fromtimestamp(session["updated"] / 1000, UTC)
    except OverflowError, OSError, TypeError, ValueError:
        raise ArchiveError("OpenCode session timestamps are invalid") from None
    if updated < created:
        raise ArchiveError("OpenCode session timestamps are invalid")
    return created, updated


def collect(run: CollectionRun) -> int:
    """Export every session whose lifecycle intersects the Collection Range."""

    opencode_version = _run_opencode(["--version"]).decode("utf-8", "replace").strip()
    list_started_at = _observation_time()
    server, server_url = _start_server()
    try:
        sessions = _discover_sessions(server_url, run)
    finally:
        _stop_server(server)
    list_completed_at = _observation_time()
    selected: list[dict[str, Any]] = []
    for session in sessions:
        created, updated = _session_interval(session)
        if created < run.collection_range.end and run.collection_range.start <= updated:
            selected.append(session)

    for session in selected:
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

    run.publish(
        {
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
    return len(selected)
