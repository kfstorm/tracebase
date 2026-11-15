"""One behavioral contract, executed only inside isolated real-CLI containers."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tracebase.archive import load_published_archive
from tracebase.context import ContextRequest, extract_context
from tracebase.summary_opencode import HostOpenCode, prepare_state

START = 1767225600000
END = START + 3600000


def cli(*args, cwd=None):
    result = subprocess.run(
        ["opencode", *args], cwd=cwd, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def api(operation, body=None, **params):
    args = ["--standalone", "api", operation]
    for key, value in params.items():
        args += ["--param", f"{key}={value}"]
    if body is not None:
        args += ["--data", json.dumps(body)]
    return json.loads(cli(*args))


def initialize(major, tmp_path):
    ids = {}
    for index, name in enumerate(
        (
            "root",
            "child",
            "archived",
            "archive-only",
            "at-end",
            "before-start",
            "synthetic-only",
            "compaction-only",
            "tool-only",
        )
    ):
        directory = f"/workspace/example-{index % 2}"
        Path(directory).mkdir(exist_ok=True)
        if not Path(directory, ".git").exists():
            subprocess.run(["git", "init", directory], check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    directory,
                    "-c",
                    "user.name=Synthetic User",
                    "-c",
                    "user.email=user@example.com",
                    "commit",
                    "--allow-empty",
                    "-m",
                    f"Synthetic project {index}",
                ],
                check=True,
                capture_output=True,
            )
        sid = f"ses_synthetic{index:04d}"
        ids[name] = sid
        updated = END if name == "at-end" else START
        if name == "before-start":
            updated = START - 1
        time = {"created": START - 10000, "updated": updated}
        if name in {"archived", "archive-only"}:
            time["archived"] = updated
        info = {"id": sid, "title": name, "time": time}
        if name == "child":
            info["parentID"] = ids["root"]
        message_time = START - 1000 if name == "archive-only" else START
        if major == 2:
            template = api("session.create", {"location": {"directory": directory}})[
                "data"
            ]
            info = template | info
            messages = [
                {
                    "id": f"msg_{index:04d}user",
                    "type": "user",
                    "text": "Synthetic question",
                    "time": {"created": message_time},
                },
                {
                    "id": f"msg_{index:04d}answer",
                    "type": "assistant",
                    "agent": "build",
                    "model": {"providerID": "example", "id": "synthetic"},
                    "content": [{"type": "text", "text": "Synthetic answer"}],
                    "time": {
                        "created": message_time + 1,
                        "completed": message_time + 1,
                    },
                },
            ]
            extras = [
                {
                    "id": f"msg_{index:04d}synthetic",
                    "type": "synthetic",
                    "text": "Excluded synthetic",
                    "time": {"created": START + 2},
                },
                {
                    "id": f"msg_{index:04d}compact",
                    "type": "compaction",
                    "status": "completed",
                    "reason": "manual",
                    "summary": "Excluded summary",
                    "time": {"created": START + 3},
                },
                {
                    "id": f"msg_{index:04d}tool",
                    "type": "assistant",
                    "agent": "build",
                    "model": {"providerID": "example", "id": "synthetic"},
                    "content": [
                        {
                            "type": "tool",
                            "id": "synthetic-call",
                            "name": "example",
                            "state": {
                                "status": "completed",
                                "input": {},
                                "content": [
                                    {"type": "text", "text": "Excluded tool output"}
                                ],
                            },
                            "time": {"created": START + 4},
                        }
                    ],
                    "time": {"created": START + 4, "completed": START + 4},
                },
            ]
            messages = scenario_messages(name, messages, extras)
            api(
                "experimental.session.import",
                {
                    "info": info,
                    "messages": messages,
                    "location": {"directory": directory},
                },
            )
        else:
            info |= {
                "projectID": "global",
                "directory": directory,
                "version": "1.0.0",
                "slug": name,
            }
            messages = []
            for role in ("user", "assistant"):
                mid = f"msg_{index:04d}{role}"
                messages.append(
                    {
                        "info": {
                            "id": mid,
                            "sessionID": sid,
                            "role": role,
                            "time": {"created": message_time + (role == "assistant")},
                            "agent": "build",
                            "model": {"providerID": "example", "modelID": "synthetic"},
                            "parentID": f"msg_{index:04d}user",
                            "modelID": "synthetic",
                            "providerID": "example",
                            "mode": "build",
                            "path": {"cwd": directory, "root": directory},
                            "cost": 0,
                            "tokens": {
                                "input": 0,
                                "output": 0,
                                "reasoning": 0,
                                "cache": {"read": 0, "write": 0},
                            },
                        },
                        "parts": [
                            {
                                "id": f"prt_{index:04d}{role}",
                                "sessionID": sid,
                                "messageID": mid,
                                "type": "text",
                                "text": "Synthetic question"
                                if role == "user"
                                else "Synthetic answer",
                            }
                        ],
                    }
                )
            extras = []
            for extra_index, kind in enumerate(("synthetic", "compaction", "tool")):
                extra = json.loads(json.dumps(messages[1]))
                mid = f"msg_{index:04d}extra{extra_index}"
                extra["info"]["id"] = mid
                extra["info"]["time"]["created"] = START + 2 + extra_index
                part = extra["parts"][0]
                part.update(
                    {"id": f"prt_{index:04d}extra{extra_index}", "messageID": mid}
                )
                part["text"] = f"Excluded {kind}"
                if kind == "synthetic":
                    part["synthetic"] = True
                elif kind == "compaction":
                    extra["info"]["summary"] = True
                else:
                    part.update(
                        {
                            "type": "tool",
                            "callID": "synthetic-call",
                            "tool": "example",
                            "state": {
                                "status": "completed",
                                "input": {},
                                "output": "Excluded tool output",
                                "title": "Synthetic tool",
                                "metadata": {},
                                "time": {"start": START, "end": START + 4},
                            },
                        }
                    )
                extras.append(extra)
            messages = scenario_messages(name, messages, extras)
            path = tmp_path / f"{name}.json"
            path.write_text(json.dumps({"info": info, "messages": messages}))
            cli("import", str(path), cwd=directory)
        set_updated(major, sid, updated)
    return ids


def scenario_messages(name, messages, extras):
    only = {"synthetic-only": 0, "compaction-only": 1, "tool-only": 2}
    return [extras[only[name]]] if name in only else messages + extras


def isolated_database():
    assert Path("/.dockerenv").exists()
    assert os.environ["XDG_DATA_HOME"] == "/isolated/data"
    databases = list(Path("/isolated/data").rglob("*.db"))
    assert len(databases) == 1
    return databases[0]


def set_updated(major, sid, updated):
    # Imports rewrite updated time. Freeze only the disposable fixture database;
    # the collector itself always uses the real CLI/API, never SQLite.
    table = "session_v2" if major == 2 else "session"
    with sqlite3.connect(isolated_database()) as connection:
        assert (
            connection.execute(
                f"UPDATE {table} SET time_updated=? WHERE id=?", (updated, sid)
            ).rowcount
            == 1
        )


@pytest.mark.skipif(
    not Path("/integration/entrypoint.sh").exists(),
    reason="Run scripts/test-opencode-integration.sh (both real versions in Docker)",
)
def test_real_collection_and_offline_context(tmp_path):
    assert os.environ["HOME"].startswith("/isolated/")
    major = int(cli("--version").decode().removeprefix("opencode v").split(".")[0])
    ids = initialize(major, tmp_path)
    archive = tmp_path / "archive"
    result = collect_cli(archive)
    assert result.returncode == 0, result.stderr
    runs = load_published_archive(archive)
    snapshots = {item.manifest["source_id"]: item for item in runs[0].snapshots}
    if major == 2:
        coverage = runs[0].manifest["coverage"]
        assert coverage["session_discovery_page_count"] > 1
        assert (
            len(coverage["session_discovery_requests"])
            == coverage["session_discovery_page_count"]
        )
        assert coverage["listed_session_count"] >= len(ids)
        assert (
            coverage["observation_window"]["from"]
            <= coverage["observation_window"]["to"]
        )
    assert set(snapshots) == {
        sid for name, sid in ids.items() if name not in {"at-end", "before-start"}
    }
    context = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"), runs
    )
    assert {item.projection.session_id for item in context.items} == {
        ids["root"],
        ids["archived"],
    }
    assert {item.projection.session["working_directory"] for item in context.items} == {
        "/workspace/example-0"
    }
    for item in context.items:
        assert [
            (turn.role, turn.text) for turn in item.projection.dialogue.activity
        ] == [("user", "Synthetic question"), ("assistant", "Synthetic answer")]
    boundary = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.001Z"), runs
    )
    assert all(
        [turn.role for turn in item.projection.dialogue.activity] == ["user"]
        for item in boundary.items
    )
    verify_native_evidence(major, snapshots)
    verify_recollection(major, ids, archive)
    verify_failed_publication(tmp_path / "failed-archive", major)


def collect_cli(
    archive, from_text="2026-01-01T00:00:00Z", to_text="2026-01-01T01:00:00Z", fault=""
):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import tracebase.opencode_v2 as v2",
                    "v2.PAGE_LIMIT = 2",
                    fault,
                    "from tracebase import main",
                    "main()",
                ]
            ),
            "collect",
            "opencode",
            "--archive",
            str(archive),
            "--instance-id",
            "synthetic-instance",
            "--from",
            from_text,
            "--to",
            to_text,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def verify_native_evidence(major, snapshots):
    directories = set()
    for sid, snapshot in snapshots.items():
        if major == 1:
            raw = snapshot.evidence["session.json"]
            assert raw == cli("export", sid)
            payload = json.loads(raw)
            directories.add(payload["info"]["directory"])
            assert payload["messages"]
        else:
            requests = snapshot.manifest["metadata"]["requests"]
            assert requests[0] == {
                "path": "session.json",
                "operation": "session.get",
                "parameters": {"sessionID": sid},
            }
            assert all(
                request["operation"] in {"session.get", "session.message.list"}
                for request in requests
            )
            assert set(snapshot.evidence) == {request["path"] for request in requests}
            assert all(
                path == "session.json" or path.startswith("messages/")
                for path in snapshot.evidence
            )
            for request in requests:
                args = ["--standalone", "api", request["operation"]]
                for key, value in request["parameters"].items():
                    args += ["--param", f"{key}={value}"]
                raw = snapshot.evidence[request["path"]]
                assert raw == cli(*args)
                if request["operation"] == "session.get":
                    directories.add(json.loads(raw)["data"]["location"]["directory"])
            if sid.endswith("0000"):
                messages = [
                    request
                    for request in requests
                    if request["operation"] == "session.message.list"
                ]
                assert len(messages) > 1
                expected_cursor = None
                identifiers = []
                for request in messages:
                    assert request["parameters"].get("cursor") == expected_cursor
                    page = json.loads(snapshot.evidence[request["path"]])
                    identifiers.extend(item["id"] for item in page["data"])
                    expected_cursor = page["cursor"].get("next")
                assert expected_cursor is None
                assert {
                    f"msg_0000{suffix}"
                    for suffix in ("user", "answer", "synthetic", "compact", "tool")
                } <= set(identifiers)
                # Compare with a larger independent request. The API can return
                # a next cursor even on a short page, followed by an empty page.
                native = api(
                    "session.message.list", sessionID=sid, limit="200", order="asc"
                )
                assert identifiers == [item["id"] for item in native["data"]]
                if cursor := native["cursor"].get("next"):
                    terminal = api(
                        "session.message.list",
                        sessionID=sid,
                        limit="200",
                        cursor=cursor,
                    )
                    assert terminal["data"] == []
                    assert terminal["cursor"].get("next") is None
    assert directories == {"/workspace/example-0", "/workspace/example-1"}


def verify_recollection(major, ids, archive):
    for sid in ids.values():
        set_updated(major, sid, END)
    result = collect_cli(archive, "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z")
    assert result.returncode == 0, result.stderr
    runs = load_published_archive(archive)
    assert len(runs) == 2
    context = extract_context(
        ContextRequest.parse("2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z"), runs
    )
    assert not context.items
    # An end later than both observations selects the latest Snapshot per Item.
    context = extract_context(
        ContextRequest.parse("2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"), runs
    )
    assert all(
        item.snapshot.run["run_id"] == runs[-1].manifest["run_id"]
        for item in context.items
    )


def verify_failed_publication(archive, major):
    # Delete the second hydrated fixture after discovery: the ensuing export/API
    # request really fails against OpenCode after one Snapshot has been staged.
    fault = """
import sqlite3
from tracebase.progress import ProgressReporter
original = ProgressReporter.emit
count = 0
def emit(self, event):
    global count
    original(self, event)
    if event.task_id == "opencode.hydrate" and event.phase == "starting":
        count += 1
        if count == 2:
            with sqlite3.connect(DATABASE) as connection:
                connection.execute("DELETE FROM TABLE WHERE id=?", (event.current,))
ProgressReporter.emit = emit
""".replace("DATABASE", repr(str(isolated_database()))).replace(
        "TABLE", "session_v2" if major == 2 else "session"
    )
    result = collect_cli(archive, "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z", fault)
    assert result.returncode != 0
    assert not list((archive / "runs").glob("*"))
    assert list((archive / ".staging").glob("*/snapshots/session/*/snapshot.json"))


@pytest.mark.skipif(
    not Path("/integration/entrypoint.sh").exists(),
    reason="Native authentication protocol is tested only in the Docker suite",
)
def test_native_same_major_credentials_with_synthetic_key(tmp_path):
    assert Path("/.dockerenv").exists()
    version = cli("--version").decode().strip().removeprefix("opencode v")
    if version.startswith("1."):
        auth = Path(os.environ["XDG_DATA_HOME"]) / "opencode/auth.json"
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text('{"synthetic":{"type":"api","key":"synthetic-secret"}}')
        state = tmp_path / "state"
        prepare_state(state, HostOpenCode("opencode", version, 1), "unused-image")
        assert json.loads(
            (state / "share/opencode/auth.json").read_text()
        ) == json.loads(auth.read_text())
    else:
        credentials = json.dumps(
            [
                {
                    "id": "cred_synthetic",
                    "integrationID": "synthetic",
                    "label": "Synthetic key",
                    "active": True,
                    "value": {"type": "key", "key": "synthetic-secret"},
                }
            ]
        ).encode()
        for index in range(2):
            environment = os.environ | {
                "XDG_DATA_HOME": str(tmp_path / f"credential-state-{index}")
            }
            imported = subprocess.run(
                ["opencode", "--standalone", "auth", "import"],
                input=credentials,
                capture_output=True,
                env=environment,
                check=False,
            )
            assert imported.returncode == 0
            exported = subprocess.run(
                ["opencode", "--standalone", "auth", "export"],
                capture_output=True,
                env=environment,
                check=False,
            )
            assert exported.returncode == 0
            assert json.loads(exported.stdout) == json.loads(credentials)
            credentials = exported.stdout
