import hashlib
import json
import urllib.request
from pathlib import Path

import pytest

from tracebase.archive import (
    Archive,
    CollectionRange,
    CollectionRun,
    Snapshot,
    encode_path_id,
)
from tracebase.attribution import (
    AttributionError,
    AttributionMode,
    source_attribution_mode,
)
from tracebase.context import (
    ContextError,
    ContextRequest,
    extract_context,
    generate_context,
    load_archive,
    select_observation,
)
from tracebase.github_context import github_user_work_record_ids


def request() -> ContextRequest:
    return ContextRequest.parse(
        "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00"
    )


def run(
    archive: Archive,
    run_id: str = "run-1",
    source_kind: str = "opencode",
    from_text: str = "2026-01-01T00:00:00+08:00",
    to_text: str = "2026-01-02T00:00:00+08:00",
    effective_options: dict[str, object] | None = None,
) -> CollectionRun:
    return CollectionRun(
        archive,
        source_kind,
        "instance-1" if source_kind == "opencode" else "actor-node",
        CollectionRange.parse(from_text, to_text),
        "test",
        effective_options or {},
        run_id=run_id,
    )


def message(
    message_id: str,
    created: str,
    parts: list[dict[str, object]] | None = None,
    role: str = "user",
) -> dict[str, object]:
    return {"id": message_id, "created": created, "role": role, "parts": parts or []}


def bash_tool(end: str | None = None) -> dict[str, object]:
    time_data: dict[str, str] = {"start": "2026-01-01T01:00:00Z"}
    if end is not None:
        time_data["end"] = end
    return {
        "id": "bash",
        "type": "tool",
        "tool": "bash",
        "state": {"status": "completed" if end else "running", "time": time_data},
    }


def append_user_assistant_turn(messages: list[dict[str, object]], index: int) -> None:
    messages.extend(
        [
            message(
                f"user-{index}",
                f"2025-12-31T0{index}:00:00Z",
                [{"type": "text", "text": f"user-{index}"}],
            ),
            message(
                f"assistant-{index}",
                f"2025-12-31T0{index}:30:00Z",
                [{"type": "text", "text": f"assistant-{index}"}],
                role="assistant",
            ),
        ]
    )


def session(
    session_id: str,
    *,
    messages: list[dict[str, object]] | None = None,
    parent_id: str | None = None,
    title: str = "Trace work",
    directory: str = "/home/tester/dev/example/project",
) -> dict[str, object]:
    info: dict[str, object] = {
        "id": session_id,
        "title": title,
        "directory": directory,
    }
    payload: dict[str, object] = {
        "id": session_id,
        "info": info,
        "messages": messages or [],
    }
    if parent_id is not None:
        payload["parentID"] = parent_id
    return payload


def publish_opencode(
    archive: Archive,
    payload: dict[str, object],
    run_id: str,
    *,
    project: dict[str, object] | None = None,
    from_text: str | None = None,
    to_text: str | None = None,
    observation_window: dict[str, str] | None = None,
) -> None:
    current = run(
        archive,
        run_id,
        from_text=from_text
        or (
            "2026-01-02T00:00:00+08:00"
            if run_id == "child-run"
            else "2026-01-01T00:00:00+08:00"
        ),
        to_text=to_text
        or (
            "2026-01-03T00:00:00+08:00"
            if run_id == "child-run"
            else "2026-01-02T00:00:00+08:00"
        ),
    )
    session_id = str(payload["id"])
    snapshot = current.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            session_id,
            observation_window
            if observation_window is not None
            else current.collection_range.as_manifest(),
            tuple(
                {"path": path}
                for path in (
                    ("session.json", "project.json")
                    if project is not None
                    else ("session.json",)
                )
            ),
            metadata={
                "session": {
                    "id": session_id,
                    "directory": "/home/tester/dev/example/project",
                }
            },
        )
    )
    current.write_evidence(snapshot, "session.json", json.dumps(payload).encode())
    if project is not None:
        current.write_evidence(snapshot, "project.json", json.dumps(project).encode())
    current.publish({"selected_session_count": 1})


def publish_github(
    archive: Archive,
    source_id: str,
    evidence: dict[str, object | str],
    *,
    object_kind: str = "pull-request",
    run_id: str = "github-run",
    from_text: str = "2026-01-01T00:00:00+08:00",
    to_text: str = "2026-01-02T00:00:00+08:00",
    effective_options: dict[str, object] | None = None,
    observation_window: dict[str, str] | None = None,
) -> None:
    current = run(
        archive,
        run_id,
        "github",
        from_text,
        to_text,
        effective_options,
    )
    snapshot = current.write_snapshot(
        Snapshot(
            "github",
            object_kind,
            source_id,
            observation_window
            if observation_window is not None
            else current.collection_range.as_manifest(),
            tuple({"path": name} for name in evidence),
        )
    )
    for name, value in evidence.items():
        content = value if isinstance(value, str) else json.dumps(value)
        current.write_evidence(snapshot, name, content.encode())
    current.publish({"selected_artifacts": 1, "pagination_complete": True})


def publish_chatgpt(archive: Archive, source_id: str = "conversation-1") -> None:
    current = run(archive, "chatgpt-run", "chatgpt")
    snapshot = current.write_snapshot(
        Snapshot(
            "chatgpt",
            "conversation",
            source_id,
            current.collection_range.as_manifest(),
            ({"path": "conversation.json"},),
        )
    )
    current.write_evidence(
        snapshot,
        "conversation.json",
        json.dumps({"id": source_id, "title": "Private conversation"}).encode(),
    )
    current.publish({"selected_conversation_count": 1})


def files(output: Path) -> set[str]:
    return {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }


def text(output: Path, suffix: str) -> str:
    path = next(output.rglob(suffix))
    return path.read_text()


def github_base(source_id: str = "PR_1", number: int = 1) -> dict[str, object]:
    return {
        "node_id": source_id,
        "repository_url": "https://api.github.com/repos/example/project",
        "number": number,
        "title": "Context Output",
        "body": "Description",
        "state": "open",
        "user": {"login": "author"},
        "created_at": "2025-12-31T16:00:00Z",
        "updated_at": "2025-12-31T16:00:00Z",
    }


def write_github_profile(
    archive: Archive,
    login: str = "tracked-user",
    *,
    scope_id: str = "actor-node",
    emails: list[str] | None = None,
    email_pages: list[list[str]] | None = None,
    profile: dict[str, object] | None = None,
) -> Path:
    profile_root = archive.root / "profiles" / "github" / encode_path_id(scope_id)
    profile_root.mkdir(parents=True, exist_ok=True)
    user = profile or {
        "id": 123,
        "login": login,
        "node_id": scope_id,
        "profile_field": "preserved",
    }
    pages = email_pages or [emails or ["private@example.com"]]
    response_files = ["user.json"] + [
        f"emails.{index:03d}.json" for index in range(1, len(pages) + 1)
    ]
    (profile_root / "profile.json").write_text(
        json.dumps(
            {
                "format_version": 2,
                "provider": "github",
                "response_files": response_files,
                "scope_id": scope_id,
                "synced_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (profile_root / "user.json").write_text(json.dumps(user), encoding="utf-8")
    for index, page in enumerate(pages, start=1):
        (profile_root / f"emails.{index:03d}.json").write_text(
            json.dumps([{"email": email} for email in page]), encoding="utf-8"
        )
    return profile_root


def activity_evidence() -> dict[str, object]:
    return {
        "comments.001.json": [
            {"id": 1, "body": "activity", "created_at": "2026-01-01T01:00:00Z"}
        ]
    }


def opencode_activity(
    archive: Archive, tmp_path: Path, parts: list[dict[str, object]], created: str
) -> str:
    publish_opencode(
        archive,
        session("root", messages=[message("m", created, parts)]),
        "run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    return text(output, "activity.md")


def opencode_background(
    archive: Archive, tmp_path: Path, messages: list[dict[str, object]]
) -> str:
    publish_opencode(archive, session("root", messages=messages), "run")
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    return text(output, "background.md")


def publish_user_text_pair(archive: Archive, value: str) -> None:
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "earlier",
                    "2025-12-31T01:00:00Z",
                    [{"type": "text", "text": value}],
                ),
                message(
                    "current",
                    "2026-01-01T01:00:00Z",
                    [{"type": "text", "text": value}],
                ),
            ],
        ),
        "run",
    )


def assert_outputs_equal(one: Path, two: Path) -> None:
    assert files(one) == files(two)
    assert b"".join(
        path.read_bytes() for path in sorted(one.rglob("*")) if path.is_file()
    ) == b"".join(
        path.read_bytes() for path in sorted(two.rglob("*")) if path.is_file()
    )


def github_output(
    archive: Archive,
    tmp_path: Path,
    evidence: dict[str, object | str],
    pull_request: dict[str, object] | None = None,
    effective_options: dict[str, object] | None = None,
    with_profile: bool = True,
) -> Path:
    payload = {"node_id": "PR_1", **(pull_request or {})}
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": github_base(),
            "pull-request.json": payload,
            **evidence,
        },
        effective_options=effective_options,
    )
    if with_profile:
        profile_root = (
            archive.root / "profiles" / "github" / encode_path_id("actor-node")
        )
        if not profile_root.exists():
            write_github_profile(archive)
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    return output


def test_selection_chooses_first_observation_at_or_after_request_end(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    for run_id, marker, from_text, to_text, observed_to in (
        (
            "before",
            "before",
            "2025-12-28T00:00:00+00:00",
            "2025-12-29T00:00:00+00:00",
            "2026-01-01T10:00:00Z",
        ),
        (
            "first-future",
            "first future",
            "2025-12-29T00:00:00+00:00",
            "2025-12-30T00:00:00+00:00",
            "2026-01-01T17:00:00Z",
        ),
        (
            "second-future",
            "second future",
            "2025-12-30T00:00:00+00:00",
            "2025-12-31T00:00:00+00:00",
            "2026-01-01T18:00:00Z",
        ),
    ):
        publish_opencode(
            archive,
            session(
                "root",
                messages=[
                    message(
                        marker,
                        "2026-01-01T01:00:00Z",
                        [{"type": "text", "text": marker}],
                    )
                ],
            ),
            run_id,
            from_text=from_text,
            to_text=to_text,
            observation_window={
                "from": "2026-01-01T09:00:00Z",
                "to": observed_to,
            },
        )

    result = extract_context(request(), load_archive(archive.root), archive.root)
    item = result.items[0]
    assert item.snapshot.run["run_id"] == "first-future"
    assert item.opencode is not None
    assert [message["id"] for message in item.opencode.messages] == ["first future"]


def test_selection_chooses_latest_observation_before_request_end(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    for run_id, marker, from_text, to_text, observed_to in (
        (
            "early",
            "early",
            "2025-12-28T00:00:00+00:00",
            "2025-12-29T00:00:00+00:00",
            "2026-01-01T10:00:00Z",
        ),
        (
            "latest",
            "latest",
            "2025-12-29T00:00:00+00:00",
            "2025-12-30T00:00:00+00:00",
            "2026-01-01T15:00:00Z",
        ),
    ):
        publish_opencode(
            archive,
            session(
                "root",
                messages=[message(marker, "2026-01-01T01:00:00Z")],
            ),
            run_id,
            from_text=from_text,
            to_text=to_text,
            observation_window={
                "from": "2026-01-01T09:00:00Z",
                "to": observed_to,
            },
        )

    result = extract_context(request(), load_archive(archive.root), archive.root)
    assert result.items[0].snapshot.run["run_id"] == "latest"


def test_selection_chooses_earliest_observation_for_historical_request_end(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    for run_id, from_text, to_text, observed_to in (
        (
            "aug-20",
            "2025-12-28T00:00:00+00:00",
            "2025-12-29T00:00:00+00:00",
            "2026-01-03T00:00:00Z",
        ),
        (
            "sep-01",
            "2025-12-29T00:00:00+00:00",
            "2025-12-30T00:00:00+00:00",
            "2026-01-04T00:00:00Z",
        ),
    ):
        publish_opencode(
            archive,
            session(
                "root",
                messages=[message(run_id, "2025-12-31T12:00:00Z")],
            ),
            run_id,
            from_text=from_text,
            to_text=to_text,
            observation_window={
                "from": "2026-01-02T00:00:00Z",
                "to": observed_to,
            },
        )

    historical = ContextRequest.parse("2025-12-31T00:00:00Z", "2026-01-01T00:00:00Z")
    result = extract_context(historical, load_archive(archive.root))
    assert result.items[0].snapshot.run["run_id"] == "aug-20"


def test_selection_uses_observation_window_not_collection_range(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session("root", messages=[message("first", "2026-01-01T01:00:00Z")]),
        "first",
        from_text="2026-01-01T00:00:00+00:00",
        to_text="2026-01-02T00:00:00+00:00",
        observation_window={
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T10:00:00Z",
        },
    )
    publish_opencode(
        archive,
        session("root", messages=[message("second", "2026-01-01T01:00:00Z")]),
        "second",
        from_text="2026-01-02T00:00:00+00:00",
        to_text="2026-01-03T00:00:00+00:00",
        observation_window={
            "from": "2026-01-01T11:00:00Z",
            "to": "2026-01-01T15:00:00Z",
        },
    )

    result = extract_context(request(), load_archive(archive.root))
    assert result.items[0].snapshot.run["run_id"] == "second"


def test_selection_contract_is_shared_by_github_and_opencode(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    for source_kind, run_id, from_text, to_text in (
        (
            "opencode",
            "opencode-old",
            "2025-12-28T00:00:00+00:00",
            "2025-12-29T00:00:00+00:00",
        ),
        (
            "opencode",
            "opencode-new",
            "2025-12-29T00:00:00+00:00",
            "2025-12-30T00:00:00+00:00",
        ),
        (
            "github",
            "github-old",
            "2025-12-28T00:00:00+00:00",
            "2025-12-29T00:00:00+00:00",
        ),
        (
            "github",
            "github-new",
            "2025-12-29T00:00:00+00:00",
            "2025-12-30T00:00:00+00:00",
        ),
    ):
        window = {
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T10:00:00Z"
            if run_id.endswith("old")
            else "2026-01-01T17:00:00Z",
        }
        if source_kind == "opencode":
            publish_opencode(
                archive,
                session("root", messages=[message(run_id, "2026-01-01T01:00:00Z")]),
                run_id,
                from_text=from_text,
                to_text=to_text,
                observation_window=window,
            )
        else:
            publish_github(
                archive,
                "PR_1",
                {
                    "issue.json": github_base(),
                    "pull-request.json": {"node_id": "PR_1"},
                    "comments.001.json": [
                        {
                            "id": 1,
                            "body": run_id,
                            "created_at": "2026-01-01T01:00:00Z",
                        }
                    ],
                },
                run_id=run_id,
                from_text=from_text,
                to_text=to_text,
                observation_window=window,
            )

    write_github_profile(archive)
    result = extract_context(request(), load_archive(archive.root), archive.root)
    selected = {
        item.snapshot.manifest["source_kind"]: item.snapshot.run["run_id"]
        for item in result.items
    }
    assert selected == {"github": "github-new", "opencode": "opencode-new"}


def test_context_skips_known_chatgpt_source_without_projector(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": github_base(),
            "pull-request.json": {"node_id": "PR_1"},
            **activity_evidence(),
        },
    )
    publish_opencode(
        archive,
        session("root", messages=[message("session-message", "2026-01-01T01:00:00Z")]),
        "opencode-run",
    )
    publish_chatgpt(archive)
    write_github_profile(archive)

    output = tmp_path / "output"
    result = extract_context(request(), load_archive(archive.root), archive.root)
    generate_context(archive.root, request(), output)

    assert {item.snapshot.manifest["source_kind"] for item in result.items} == {
        "github",
        "opencode",
    }
    assert {
        item.snapshot.manifest["source_kind"]: item.attribution_mode
        for item in result.items
    } == {
        "github": AttributionMode.ACTOR_SCOPED,
        "opencode": AttributionMode.PERSONAL,
    }
    assert source_attribution_mode("chatgpt") is AttributionMode.PERSONAL
    assert "github/example/project/pull/1/overview.md" in files(output)
    assert "opencode/home/tester/dev/example/project/session/01/overview.md" in files(
        output
    )
    assert not any(path.startswith("chatgpt/") for path in files(output))


@pytest.mark.parametrize(
    ("source_kind", "object_kind"),
    (("unknown", "snapshot"), ("github", "conversation")),
)
def test_context_rejects_archive_unknown_source_or_object_kind(
    tmp_path: Path, source_kind: str, object_kind: str
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    current = run(archive, "invalid-run", source_kind)
    snapshot = current.write_snapshot(
        Snapshot(
            source_kind,
            object_kind,
            "source-1",
            current.collection_range.as_manifest(),
            ({"path": "evidence.json"},),
        )
    )
    current.write_evidence(snapshot, "evidence.json", b"{}")
    current.publish({})

    with pytest.raises(ContextError, match="unsupported context source"):
        extract_context(request(), load_archive(archive.root))


def test_selection_tie_break_is_independent_of_input_order(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    for run_id, from_text, to_text in (
        ("tie-z", "2025-12-28T00:00:00+00:00", "2025-12-29T00:00:00+00:00"),
        ("tie-a", "2025-12-29T00:00:00+00:00", "2025-12-30T00:00:00+00:00"),
    ):
        publish_opencode(
            archive,
            session("root", messages=[message(run_id, "2026-01-01T01:00:00Z")]),
            run_id,
            from_text=from_text,
            to_text=to_text,
            observation_window={
                "from": "2026-01-01T09:00:00Z",
                "to": "2026-01-01T17:00:00Z",
            },
        )

    snapshots = tuple(
        snapshot for run in load_archive(archive.root) for snapshot in run.snapshots
    )
    assert (
        select_observation(tuple(reversed(snapshots)), request().end).run["run_id"]
        == "tie-a"
    )


def test_empty_output_has_only_useful_index_without_front_matter(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    output = tmp_path / "output"
    generate_context(archive, request(), output)
    assert files(output) == {"index.md"}
    index = (output / "index.md").read_text()
    assert "Requested interval" in index
    assert "source_scope_id" not in index
    assert "coverage" not in index.lower()


def test_archive_evidence_is_read_only_when_accessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T01:00:00Z")]),
        "run",
    )

    read_paths: list[Path] = []
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        read_paths.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    runs = load_archive(archive.root)

    assert read_paths == []
    snapshot = runs[0].snapshots[0]
    assert snapshot.evidence["session.json"]
    assert read_paths == [snapshot.root / "session.json"]


def test_empty_removed_run_directory_is_tolerated(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    (archive / "runs" / "removed-run" / "snapshots").mkdir(parents=True)

    output = tmp_path / "output"
    generate_context(archive, request(), output)

    assert files(output) == {"index.md"}


def test_empty_published_run_without_snapshots_directory_is_tolerated(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    published = run(archive).publish({})
    (published / "snapshots").rmdir()

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    assert files(output) == {"index.md"}


def test_nonempty_unregistered_run_content_fails(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    invalid_run = archive / "runs" / "invalid-run"
    (invalid_run / "snapshots").mkdir(parents=True)
    (invalid_run / "unregistered.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ContextError, match="unregistered entries"):
        load_archive(archive)


def test_missing_declared_evidence_fails_before_publication(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    current = run(archive)
    snapshot = current.write_snapshot(
        Snapshot(
            "opencode",
            "session",
            "session-1",
            current.collection_range.as_manifest(),
            ({"path": "session.json"},),
        )
    )
    current.write_evidence(
        snapshot, "session.json", b'{"id":"session-1","messages":[]}'
    )
    current.publish({})
    evidence = (
        archive.root
        / "runs"
        / current.run_id
        / "snapshots"
        / "session"
        / encode_path_id("session-1")
        / "session.json"
    )
    evidence.unlink()

    with pytest.raises(ContextError, match="missing"):
        generate_context(archive.root, request(), tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_github_uses_natural_path_and_heading(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(archive, tmp_path, activity_evidence())
    assert "github/example/project/pull/1/overview.md" in files(output)
    overview = text(output, "overview.md")
    assert "# example/project PR #1" in overview
    assert "PR_1" not in overview
    assert "## Tracked account" in overview
    assert "locally synced identity profile" in overview
    assert "Tracked GitHub account:" not in (output / "index.md").read_text()
    assert "github/example/project" in (output / "index.md").read_text()


def test_issue_uses_issue_path_and_domain_author_wording(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_github(
        archive,
        "I_1",
        {
            "issue.json": {**github_base("I_1"), "number": 7},
            **activity_evidence(),
        },
        object_kind="issue",
    )
    write_github_profile(archive)
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    overview = text(output, "overview.md")
    assert "github/example/project/issue/7/overview.md" in files(output)
    assert "Author: @author" in overview
    assert "Actor" not in overview


def test_comment_aliases_render_once_and_equal_bodies_remain_distinct(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "comments.001.json": [
            {
                "id": 10,
                "node_id": "IC_10",
                "body": "same",
                "user": {"login": "a"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 11,
                "node_id": "IC_11",
                "body": "same",
                "user": {"login": "b"},
                "created_at": "2026-01-01T02:00:00Z",
            },
        ],
        "timeline.001.json": [
            {
                "id": 10,
                "node_id": "IC_10",
                "event": "commented",
                "body": "same",
                "actor": {"login": "a"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 11,
                "node_id": "IC_11",
                "event": "commented",
                "body": "same",
                "actor": {"login": "b"},
                "created_at": "2026-01-01T02:00:00Z",
            },
        ],
    }
    output = github_output(archive, tmp_path, evidence)
    activity = text(output, "activity.md")
    assert activity.count("same") == 2
    assert activity.count("**@a") == 1
    assert activity.count("**@b") == 1


def test_review_threads_are_materialized_and_unthreaded_comments_remain(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "review-comments.001.json": [
            {
                "id": 20,
                "node_id": "IC_20",
                "body": "first",
                "user": {"login": "reviewer"},
                "path": "src/foo.py",
                "line": 120,
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 21,
                "node_id": "IC_21",
                "body": "reply",
                "user": {"login": "author"},
                "path": "src/foo.py",
                "line": 120,
                "created_at": "2026-01-01T02:00:00Z",
            },
            {
                "id": 22,
                "node_id": "IC_22",
                "body": "loose",
                "user": {"login": "reviewer"},
                "path": "src/bar.py",
                "line": 3,
                "created_at": "2026-01-01T03:00:00Z",
            },
        ],
        "review-threads.001.json": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "THREAD_1",
                                    "comments": {
                                        "nodes": [{"id": "IC_20"}, {"id": "IC_21"}]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        },
    }
    output = github_output(archive, tmp_path, evidence)
    activity = text(output, "activity.md")
    assert "Review thread · src/foo.py:120" in activity
    assert activity.index("first") < activity.index("reply")
    assert "loose" in activity
    assert activity.count("first") == 1
    assert activity.count("reply") == 1


def test_review_thread_classifies_rest_comments_by_graphql_canonical_id(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "review-comments.001.json": [
                {
                    "id": 776172573,
                    "node_id": "PRRC_TRACKED",
                    "body": "tracked inline review",
                    "user": {"login": "tracked-user"},
                    "path": "src/example.py",
                    "line": 12,
                    "created_at": "2026-01-01T01:00:00Z",
                },
                {
                    "id": 776172574,
                    "node_id": "PRRC_COLLABORATOR",
                    "body": "collaborator inline review",
                    "user": {"login": "collaborator"},
                    "path": "src/example.py",
                    "line": 12,
                    "created_at": "2026-01-01T02:00:00Z",
                },
                {
                    "id": 776172575,
                    "node_id": "PRRC_TRACKED_FOLLOWUP",
                    "body": "tracked follow-up",
                    "user": {"login": "tracked-user"},
                    "path": "src/example.py",
                    "line": 12,
                    "created_at": "2026-01-01T03:00:00Z",
                },
            ],
            "review-threads.001.json": {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "PRRT_THREAD",
                                        "comments": {
                                            "nodes": [
                                                {"id": "PRRC_TRACKED"},
                                                {"id": "PRRC_COLLABORATOR"},
                                                {"id": "PRRC_TRACKED_FOLLOWUP"},
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        },
        effective_options={"actor_login": "tracked-user"},
    )

    activity = text(output, "activity.md")
    assert activity.count("Review thread") == 1
    assert "#### User work" not in activity
    assert "#### Context-only evidence" not in activity
    assert (
        activity.index("tracked inline review")
        < activity.index("collaborator inline review")
        < activity.index("tracked follow-up")
    )
    assert activity.count("[User work]") == 2
    assert activity.count("[Context only]") >= 1


def test_active_review_thread_separates_earlier_and_future_comments(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "review-comments.001.json": [
            {
                "id": 19,
                "node_id": "IC_19",
                "body": "before",
                "user": {"login": "reviewer"},
                "created_at": "2025-12-31T15:00:00Z",
            },
            {
                "id": 20,
                "node_id": "IC_20",
                "body": "during",
                "user": {"login": "reviewer"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 21,
                "node_id": "IC_21",
                "body": "after",
                "user": {"login": "author"},
                "created_at": "2026-01-02T01:00:00Z",
            },
        ],
        "review-threads.001.json": {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "THREAD_1",
                                    "comments": {
                                        "nodes": [
                                            {"id": "IC_19"},
                                            {"id": "IC_20"},
                                            {"id": "IC_21"},
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        },
    }
    output = github_output(archive, tmp_path, evidence)
    activity = text(output, "activity.md")

    assert "### Earlier context" in activity
    assert "before" in activity
    assert "during" in activity
    assert "after" not in activity
    assert not (output / "github/example/project/pull/1/background.md").exists()


def test_diff_is_only_in_diff_patch(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            **activity_evidence(),
            "pull-request.diff": "diff --git a/a b/a\n+line\n",
        },
    )
    assert "github/example/project/pull/1/diff.patch" in files(output)
    diff = text(output, "diff.patch")
    assert "diff --git" in diff
    assert "diff --git" not in text(output, "overview.md")


def test_diff_patch_preserves_raw_bytes(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    raw_diff = b"diff --git a/a b/a\n+trailing  \n\n"
    output = github_output(
        archive,
        tmp_path,
        {"pull-request.diff": raw_diff.decode("utf-8")},
    )

    diff_path = next(output.rglob("diff.patch"))
    assert diff_path.read_bytes() == raw_diff


def test_future_github_events_are_absent_and_background_is_separate(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    evidence = {
        "comments.001.json": [
            {
                "id": 1,
                "body": "earlier",
                "user": {"login": "a"},
                "created_at": "2025-12-31T15:00:00Z",
            },
            {
                "id": 2,
                "body": "today",
                "user": {"login": "b"},
                "created_at": "2026-01-01T01:00:00Z",
            },
            {
                "id": 3,
                "body": "cutoff",
                "user": {"login": "c"},
                "created_at": "2026-01-01T16:00:00Z",
            },
            {
                "id": 4,
                "body": "future",
                "user": {"login": "d"},
                "created_at": "2026-01-02T00:00:00Z",
            },
        ]
    }
    output = github_output(archive, tmp_path, evidence)
    assert "earlier" in text(output, "background.md")
    assert "today" in text(output, "activity.md")
    assert "cutoff" not in text(output, "activity.md")
    assert "future" not in text(output, "activity.md")


def test_commits_use_commit_time_buckets_and_preserve_full_messages(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "timeline.001.json": [
                {
                    "id": 1,
                    "node_id": "COMMIT_BEFORE",
                    "event": "committed",
                    "sha": "before123456789",
                    "author": {"date": "2025-12-31T15:00:00Z"},
                    "committer": {"date": "2025-12-31T15:30:00Z"},
                    "message": "Before subject\n\nBefore body",
                },
                {
                    "id": 2,
                    "node_id": "COMMIT_DURING",
                    "event": "committed",
                    "sha": "during123456789",
                    "author": {"date": "2026-01-01T02:00:00Z"},
                    "committer": {"date": "2026-01-01T01:00:00Z"},
                    "message": "During subject\n\nWhy and how",
                },
                {
                    "id": 3,
                    "node_id": "COMMIT_CUTOFF",
                    "event": "committed",
                    "sha": "cutoff123456789",
                    "committer": {"date": "2026-01-02T00:00:00Z"},
                    "message": "At cutoff",
                },
                {
                    "id": 4,
                    "node_id": "COMMIT_FUTURE",
                    "event": "committed",
                    "sha": "future123456789",
                    "committer": {"date": "2026-01-02T01:00:00Z"},
                    "message": "Future",
                },
            ]
        },
    )

    activity = text(output, "activity.md")
    background = text(output, "background.md")
    assert "Commit placement uses Git committer time" in activity
    assert activity.count("Commit placement uses Git committer time") == 1
    assert "2026-01-01 09:00 · `during1` · unknown · [Context only]" in activity
    assert (
        "- 2026-01-01 09:00 · `during1` · unknown · [Context only]\n"
        "  Authored: 2026-01-01 10:00\n"
        "  During subject\n\n  Why and how"
    ) in activity
    assert "During subject" in activity and "Why and how" in activity
    assert "before1" not in activity
    assert "cutoff1" not in activity
    assert "future1" not in activity
    assert "Commit placement uses Git committer time" in background
    assert "2025-12-31 23:30 · `before1` · unknown · [Context only]" in background
    assert "Before subject" in background and "Before body" in background
    assert "during1" not in background


def test_commits_keep_timeline_order_and_show_author_identity_and_time(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(archive, emails=["Kai@Example.com", "private@example.com"])
    output = github_output(
        archive,
        tmp_path,
        {
            "timeline.001.json": [
                {
                    "id": 1,
                    "node_id": "COMMIT_Z",
                    "event": "committed",
                    "sha": "zzzzzzz1234567",
                    "author": {
                        "name": "Kai Yang",
                        "email": "kai@example.com",
                        "date": "2025-12-31T23:00:00Z",
                    },
                    "committer": {"name": "Rebaser", "date": "2026-01-01T01:00:00Z"},
                    "message": (
                        "First in Timeline\n\n"
                        "Co-authored-by: Name <private@example.com>"
                    ),
                },
                {
                    "id": 2,
                    "node_id": "COMMIT_A",
                    "event": "committed",
                    "sha": "aaaaaaa1234567",
                    "author": {
                        "name": "mubai",
                        "email": "mubai@example.com",
                        "date": "2026-01-01T01:00:00Z",
                    },
                    "committer": {"name": "mubai", "date": "2026-01-01T01:00:00Z"},
                    "message": "Second in Timeline",
                },
                {
                    "id": 3,
                    "node_id": "COMMIT_SAME_NAME",
                    "event": "committed",
                    "sha": "same0001234567",
                    "author": {
                        "name": "Kai Yang",
                        "email": "someone-else@example.com",
                        "date": "2026-01-01T02:00:00Z",
                    },
                    "committer": {"name": "Rebaser", "date": "2026-01-01T02:00:00Z"},
                    "message": "Same name, different identity",
                },
            ]
        },
        effective_options={"actor_login": "tracked-user"},
    )

    activity = text(output, "activity.md")
    assert activity.index("zzzzz") < activity.index("aaaaaaa")
    assert "Kai Yang (tracked account)" in activity
    assert activity.count("Kai Yang (tracked account)") == 1
    assert "`same000` · Kai Yang · [Context only]\n" in activity
    assert "mubai" in activity
    assert "kai@example.com" not in activity
    assert "Co-authored-by: Name <private@example.com>" in activity
    assert "Authored: 2026-01-01 07:00" in activity
    assert "Commit placement uses Git committer time" in activity
    assert "Neither timestamp is GitHub push time" in activity


def test_commit_attribution_uses_email_evidence_and_stable_numeric_identity(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(
        archive,
        email_pages=[["first-page@example.com"], ["known@example.com"]],
    )
    commits = [
        ("page-two", "known@example.com", "Page two email"),
        ("modern", "123+renamed-user@users.noreply.github.com", "Modern"),
        ("wrong", "999+tracked-user@users.noreply.github.com", "Wrong ID"),
        ("legacy", "tracked-user@users.noreply.github.com", "Legacy"),
        ("other", "other-user@users.noreply.github.com", "Other login"),
        ("same-name", "unrelated@example.com", "Same Name"),
    ]
    timeline = [
        {
            "id": index,
            "node_id": node_id,
            "event": "committed",
            "sha": f"{index:07d}1234567",
            "author": {
                "name": "Same Name",
                "email": email,
                "date": "2026-01-01T01:00:00Z",
            },
            "committer": {"name": "Same Name", "date": "2026-01-01T01:00:00Z"},
            "message": label,
        }
        for index, (node_id, email, label) in enumerate(commits, start=1)
    ]
    output = github_output(
        archive,
        tmp_path,
        {"timeline.001.json": timeline},
        effective_options={"actor_login": "tracked-user"},
    )
    activity = text(output, "activity.md")
    assert activity.count("Same Name (tracked account)") == 3
    assert "Wrong ID" in activity and "Modern" in activity
    assert "Modern" in activity
    assert "Page two email" in activity
    assert "Wrong ID" in activity
    assert "`0000002`" in activity
    assert "`0000004`" in activity
    assert "`0000005`" in activity


def test_github_context_with_profile_generates_normally(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(archive)
    output = github_output(
        archive,
        tmp_path,
        {"comments.001.json": []},
        effective_options={"actor_login": "tracked-user"},
    )

    overview = text(output, "overview.md")
    assert "## Tracked account" in overview
    assert "- GitHub: @tracked-user" in overview


def test_github_context_fails_when_profile_is_missing(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    profile_path = archive.root / "profiles" / "github" / encode_path_id("actor-node")

    with pytest.raises(ContextError) as error:
        github_output(
            archive,
            tmp_path,
            {"comments.001.json": []},
            effective_options={"actor_login": "tracked-user"},
            with_profile=False,
        )

    message = str(error.value)
    assert "source scope actor-node was not found" in message
    assert str(profile_path) in message
    assert "tracebase identity github sync --archive" in message
    assert "private@example.com" not in message


def test_github_context_fails_when_profile_json_is_invalid(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    profile_path = write_github_profile(archive)
    (profile_path / "profile.json").write_text(
        '{"response_files":["user.json"]', encoding="utf-8"
    )
    with pytest.raises(ContextError, match="is invalid") as error:
        github_output(
            archive,
            tmp_path,
            {"comments.001.json": []},
            effective_options={"actor_login": "tracked-user"},
        )
    assert "private@example.com" not in str(error.value)


def test_github_context_fails_when_profile_schema_is_invalid(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(
        archive,
        profile={"id": "not-an-id", "login": "tracked-user", "node_id": "actor-node"},
    )

    with pytest.raises(ContextError, match="is invalid") as error:
        github_output(
            archive,
            tmp_path,
            {"comments.001.json": []},
            effective_options={"actor_login": "tracked-user"},
        )
    assert "private@example.com" not in str(error.value)


def test_github_context_uses_stable_scope_when_login_changes(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(archive, login="current-user")
    output = github_output(
        archive,
        tmp_path,
        {"comments.001.json": []},
        effective_options={"actor_login": "tracked-user"},
    )
    assert "- GitHub: @tracked-user" in text(output, "overview.md")


def test_github_context_without_selected_items_does_not_require_profile(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": {
                **github_base(),
                "created_at": "2025-12-31T15:00:00Z",
                "updated_at": "2025-12-31T15:00:00Z",
            },
            "pull-request.json": {"node_id": "PR_1"},
            "comments.001.json": [],
        },
        effective_options={"actor_login": "tracked-user"},
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    assert files(output) == {"index.md"}


def test_commit_time_falls_back_to_author_time_and_unknown_time_is_omitted(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "timeline.001.json": [
                {
                    "id": 1,
                    "node_id": "COMMIT_AUTHOR",
                    "event": "committed",
                    "sha": "author123456789",
                    "author": {"date": "2026-01-01T03:00:00Z"},
                    "message": "Author time",
                },
                {
                    "id": 2,
                    "node_id": "COMMIT_UNKNOWN",
                    "event": "committed",
                    "sha": "unknown12345678",
                    "message": "Must not be guessed",
                },
            ]
        },
    )

    activity = text(output, "activity.md")
    assert "2026-01-01 11:00 · `author1` · unknown" in activity
    assert "Authored:" not in activity
    assert "Must not be guessed" not in activity


def test_high_value_timeline_events_and_review_commit_are_rendered(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "timeline.001.json": [
                {
                    "id": 1,
                    "node_id": "FORCE",
                    "event": "head_ref_force_pushed",
                    "actor": {"login": "alice"},
                    "created_at": "2026-01-01T04:00:00Z",
                    "commit_id": "force123456789",
                },
                {
                    "id": 2,
                    "node_id": "RESTORED",
                    "event": "head_ref_restored",
                    "created_at": "2026-01-01T05:00:00Z",
                    "ref": "feature",
                },
                {
                    "id": 3,
                    "node_id": "BASE",
                    "event": "base_ref_changed",
                    "created_at": "2026-01-01T06:00:00Z",
                },
                {
                    "id": 4,
                    "node_id": "RENAME",
                    "event": "renamed",
                    "created_at": "2026-01-01T07:00:00Z",
                    "rename": {"from": "old title", "to": "new title"},
                },
                {
                    "id": 5,
                    "node_id": "DELETED",
                    "event": "head_ref_deleted",
                    "created_at": "2026-01-01T08:00:00Z",
                },
            ],
            "reviews.001.json": [
                {
                    "id": 6,
                    "user": {"login": "reviewer"},
                    "submitted_at": "2026-01-01T09:00:00Z",
                    "commit_id": "abcdef123456789",
                    "state": "CHANGES_REQUESTED",
                    "body": "Please revise this.",
                }
            ],
        },
    )

    activity = text(output, "activity.md")
    assert "Head ref force-pushed" in activity
    assert "commit force1" in activity
    assert "before" not in activity and "after" not in activity
    assert "Head ref restored" in activity
    assert "Base ref changed" in activity
    assert "Renamed old title -> new title" in activity
    assert (
        "Review by @reviewer · 2026-01-01 17:00 · [Context only] · on abcdef1 "
        "(CHANGES_REQUESTED)" in activity
    )
    assert "head_ref_deleted" not in activity


def test_force_push_does_not_remove_existing_timeline_commit_and_warns_at_limit(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "timeline.001.json": [
                {
                    "id": 1,
                    "node_id": "OLD_COMMIT",
                    "event": "committed",
                    "sha": "oldcommit123456",
                    "committer": {"date": "2026-01-01T01:00:00Z"},
                    "message": "Old commit retained",
                },
                {
                    "id": 2,
                    "node_id": "FORCE",
                    "event": "head_ref_force_pushed",
                    "created_at": "2026-01-01T02:00:00Z",
                },
            ]
        },
        pull_request={"commits": 251},
    )

    activity = text(output, "activity.md")
    assert "Old commit retained" in activity
    assert "GitHub may truncate PR commit history at 250 entries" in activity


def test_observed_timeline_commit_limit_warns_without_pull_count(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "timeline.001.json": [
                {
                    "id": index,
                    "node_id": f"COMMIT_{index}",
                    "event": "committed",
                    "sha": f"{index:07x}1234567",
                    "committer": {"date": "2026-01-01T01:00:00Z"},
                    "message": f"Commit {index}",
                }
                for index in range(250)
            ]
        },
    )

    activity = text(output, "activity.md")
    assert "Observed GitHub Timeline commit events reached 250 entries" in activity


def test_github_event_with_earlier_and_in_range_times_has_one_canonical_bucket(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "edited once",
                    "user": {"login": "a"},
                    "created_at": "2025-12-31T23:00:00Z",
                    "updated_at": "2026-01-01T01:00:00Z",
                }
            ]
        },
    )
    assert "edited once" in text(output, "activity.md")
    assert not (output / "github/example/project/pull/1/background.md").exists()


def test_github_tracked_actor_is_annotated_without_hard_coding(
    tmp_path: Path,
) -> None:
    tracked_archive = Archive(tmp_path / "tracked-archive")
    tracked_archive.root.mkdir()
    write_github_profile(tracked_archive, "tracked-user")
    publish_github(
        tracked_archive,
        "PR_1",
        {
            "issue.json": {
                **github_base(),
                "user": {"login": "tracked-user"},
            },
            "pull-request.json": {"node_id": "PR_1"},
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "tracked comment",
                    "user": {"login": "tracked-user"},
                    "created_at": "2026-01-01T01:00:00Z",
                },
                {
                    "id": 2,
                    "body": "other comment",
                    "user": {"login": "other"},
                    "created_at": "2026-01-01T02:00:00Z",
                },
            ],
            "reviews.001.json": [
                {
                    "id": 3,
                    "body": "tracked review",
                    "user": {"login": "tracked-user"},
                    "state": "APPROVED",
                    "submitted_at": "2026-01-01T03:00:00Z",
                }
            ],
            "timeline.001.json": [
                {
                    "id": 4,
                    "event": "closed",
                    "actor": {"login": "tracked-user"},
                    "created_at": "2026-01-01T04:00:00Z",
                }
            ],
        },
        effective_options={"actor_login": "tracked-user"},
    )
    tracked_output = tmp_path / "tracked-output"
    generate_context(tracked_archive.root, request(), tracked_output)
    index = (tracked_output / "index.md").read_text()
    activity = text(tracked_output, "activity.md")
    overview = text(tracked_output, "overview.md")
    assert "Tracked GitHub account: @tracked-user" in index
    assert "Author: @tracked-user (tracked account)" in overview
    assert "@tracked-user (tracked account)" in activity
    assert "@other" in activity
    assert "(tracked account)" in activity
    assert "reviews.001" not in activity
    assert "Review by @tracked-user (tracked account)" in activity
    assert "Closed by @tracked-user (tracked account)" in activity
    assert "PR_1" not in index


def test_github_timestamps_are_local_minute_precision_and_equal_update_once(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "time",
                    "user": {"login": "a"},
                    "created_at": "2025-12-31T23:59:59.123Z",
                    "updated_at": "2025-12-31T23:59:59.123Z",
                }
            ]
        },
    )
    activity = text(output, "activity.md")
    assert "2026-01-01 07:59" in activity
    assert ".123" not in activity
    assert "edited" not in activity


def test_opencode_root_session_uses_title_directory_and_compact_activity(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            title="#52 refactor",
            messages=[
                message("m", "2025-12-31T16:30:00Z", [{"type": "text", "text": "work"}])
            ],
        ),
        "root-run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert any(path.name == "activity.md" for path in output.rglob("activity.md"))
    index = (output / "index.md").read_text()
    assert "/home/tester/dev/example/project" in index
    assert "#52 refactor" in index
    assert "# #52 refactor\n" in text(output, "overview.md")
    activity = text(output, "activity.md")
    assert "Message" not in activity
    assert "\n**M ·" not in activity
    assert "2026-01-01 00:30" in activity
    assert "Context limitations" not in text(output, "overview.md")


def test_opencode_index_has_no_public_gap_section_or_obsolete_gap_kinds(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "current",
                    "2026-01-01T01:00:00Z",
                    [{"type": "text", "text": "work"}],
                )
            ],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    index = (output / "index.md").read_text()

    assert "## Gaps" not in index
    for kind in (
        "malformed-session-parent",
        "missing-session-parent",
        "cyclic-session-parent",
        "malformed-task-child",
        "missing-task-child",
        "unknown-completion",
        "no_in_range_messages",
    ):
        assert kind not in index


def test_opencode_compaction_and_synthetic_text_are_not_rendered_or_counted(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    compaction_text = "compaction transcript <｜DSML｜tool_call> must stay out"
    synthetic_text = "synthetic continuation must stay out"
    compaction = message(
        "compaction",
        "2026-01-01T00:10:00Z",
        [{"type": "text", "text": compaction_text}],
        role="assistant",
    )
    compaction["info"] = {"mode": "compaction"}
    synthetic = message(
        "synthetic",
        "2025-12-31T00:30:00Z",
        [{"type": "text", "text": synthetic_text, "synthetic": True}],
    )
    summary_text = "summary transcript must stay out"
    summary = message(
        "summary",
        "2026-01-01T00:11:00Z",
        [{"type": "text", "text": summary_text}],
        role="assistant",
    )
    summary["info"] = {"summary": True}
    agent_text = "agent transcript must stay out"
    agent = message(
        "agent",
        "2026-01-01T00:12:00Z",
        [{"type": "text", "text": agent_text}],
        role="assistant",
    )
    agent["info"] = {"agent": "compaction"}
    continuation_text = "continuation transcript must stay out"
    continuation = message(
        "continuation",
        "2026-01-01T00:13:00Z",
        [
            {
                "type": "text",
                "text": continuation_text,
                "metadata": {"compaction_continue": True},
            }
        ],
    )
    compaction_part_text = "compaction part transcript must stay out"
    compaction_part = message(
        "compaction-part",
        "2026-01-01T00:14:00Z",
        [{"type": "compaction", "text": compaction_part_text}],
        role="assistant",
    )
    hidden_texts = (
        compaction_text,
        synthetic_text,
        summary_text,
        agent_text,
        continuation_text,
        compaction_part_text,
    )
    messages: list[dict[str, object]] = [
        compaction,
        synthetic,
        summary,
        agent,
        continuation,
        compaction_part,
        *(
            message(
                f"background-{index}",
                f"2025-12-31T0{index}:00:00Z",
                [{"type": "text", "text": f"background-{index}"}],
            )
            for index in range(1, 4)
        ),
    ]
    messages.append(
        message(
            "current",
            "2026-01-01T01:00:00Z",
            [{"type": "text", "text": "current work"}],
        )
    )
    publish_opencode(archive, session("root", messages=messages), "run")

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    activity = text(output, "activity.md")
    background = text(output, "background.md")

    assert "current work" in activity
    for hidden_text in hidden_texts:
        assert hidden_text not in activity and hidden_text not in background
    for index in range(1, 4):
        assert f"background-{index}" in background


def test_opencode_mixed_synthetic_text_keeps_real_text_and_selection(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    activity = opencode_activity(
        archive,
        tmp_path,
        [
            {"type": "text", "text": "real user request"},
            {
                "type": "text",
                "text": "synthetic tool description",
                "synthetic": True,
            },
            {"type": "compaction", "text": "compaction part description"},
        ],
        "2026-01-01T01:00:00Z",
    )

    assert "real user request" in activity
    assert "synthetic tool description" not in activity
    assert "compaction part description" not in activity
    assert "Trace work" in (tmp_path / "output" / "index.md").read_text()


def test_opencode_mixed_compaction_continuation_keeps_real_text(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    activity = opencode_activity(
        archive,
        tmp_path,
        [
            {"type": "text", "text": "real review result"},
            {
                "type": "text",
                "text": "synthetic failure description",
                "metadata": {"compaction_continue": True},
            },
        ],
        "2026-01-01T01:00:00Z",
    )

    assert "real review result" in activity
    assert "synthetic failure description" not in activity


@pytest.mark.parametrize(
    "supporting_part",
    [
        {"type": "text", "text": "synthetic-only", "synthetic": True},
        {
            "type": "text",
            "text": "continuation-only",
            "metadata": {"compaction_continue": True},
        },
    ],
)
def test_opencode_pure_supporting_message_is_not_selected(
    tmp_path: Path, supporting_part: dict[str, object]
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            messages=[message("supporting", "2026-01-01T01:00:00Z", [supporting_part])],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    assert not list(output.rglob("overview.md"))
    assert not list(output.rglob("activity.md"))
    assert not list(output.rglob("background.md"))


def test_opencode_pure_supporting_message_does_not_consume_background_user_limit(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    messages = [
        message(
            f"background-{index}",
            f"2025-12-31T0{index}:00:00Z",
            [{"type": "text", "text": f"background-{index}"}],
        )
        for index in range(1, 4)
    ]
    messages.extend(
        [
            message(
                "synthetic",
                "2025-12-31T04:00:00Z",
                [{"type": "text", "text": "synthetic-only", "synthetic": True}],
            ),
            message(
                "current",
                "2026-01-01T01:00:00Z",
                [{"type": "text", "text": "current work"}],
            ),
        ]
    )
    publish_opencode(archive, session("root", messages=messages), "run")

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    background = text(output, "background.md")
    for index in range(1, 4):
        assert f"background-{index}" in background
    assert "synthetic-only" not in background


def test_opencode_non_compaction_assistant_keeps_dsml_like_text(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    assistant_text = "normal assistant <｜DSML｜tool_call> text"
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "assistant",
                    "2026-01-01T01:00:00Z",
                    [{"type": "text", "text": assistant_text}],
                    role="assistant",
                )
            ],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    activity = text(output, "activity.md")
    assert "**Assistant" in activity
    assert assistant_text in activity


def test_opencode_non_text_selection_has_local_context_limitation(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    tool = {
        "type": "tool",
        "tool": "bash",
        "state": {
            "status": "completed",
            "time": {
                "start": "2026-01-01T01:00:00Z",
                "end": "2026-01-01T01:01:00Z",
            },
        },
    }
    publish_opencode(
        archive,
        session(
            "root",
            messages=[message("tool", "2025-12-31T10:00:00Z", [tool])],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    overview = text(output, "overview.md")
    assert (
        "## Context limitations\n\n"
        "No in-range User or Assistant work text is retained for this session; "
        "it was selected by in-range non-text activity."
    ) in overview
    assert not list(output.rglob("activity.md"))
    assert "## Gaps" not in (output / "index.md").read_text()


def test_opencode_unknown_completion_is_temporal_state_not_public_gap(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    running_tool = bash_tool()
    publish_opencode(
        archive,
        session(
            "root", messages=[message("tool", "2026-01-01T01:00:00Z", [running_tool])]
        ),
        "before-completion",
        from_text="2025-12-30T00:00:00+00:00",
        to_text="2026-01-01T00:00:00+00:00",
    )

    before = extract_context(request(), load_archive(archive.root))
    before_part = before.items[0].opencode.messages[0]["parts"][0]
    assert before_part["completion"] == "unknown"

    completed_tool = bash_tool("2026-01-01T01:01:00Z")
    publish_opencode(
        archive,
        session(
            "root",
            messages=[message("tool", "2026-01-01T01:00:00Z", [completed_tool])],
        ),
        "after-completion",
        from_text="2026-01-02T00:00:00+00:00",
        to_text="2026-01-03T00:00:00+00:00",
    )

    after = extract_context(request(), load_archive(archive.root))
    after_part = after.items[0].opencode.messages[0]["parts"][0]
    assert after_part["end"] == "2026-01-01T01:01:00+00:00"
    assert "completion" not in after_part

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert "## Gaps" not in (output / "index.md").read_text()


def test_opencode_does_not_merge_a_later_observation(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    running_tool = bash_tool()
    completed_tool = bash_tool("2026-01-01T01:01:00Z")
    publish_opencode(
        archive,
        session(
            "root",
            messages=[message("turn", "2026-01-01T01:00:00Z", [running_tool])],
        ),
        "selected",
        from_text="2025-12-28T00:00:00+00:00",
        to_text="2025-12-29T00:00:00+00:00",
        observation_window={
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T17:00:00Z",
        },
    )
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message("turn", "2026-01-01T01:00:00Z", [completed_tool]),
                message(
                    "later-message",
                    "2026-01-01T02:00:00Z",
                    [{"type": "text", "text": "must not leak"}],
                ),
            ],
        ),
        "later",
        from_text="2025-12-29T00:00:00+00:00",
        to_text="2025-12-30T00:00:00+00:00",
        observation_window={
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T18:00:00Z",
        },
    )

    result = extract_context(request(), load_archive(archive.root))
    assert result.items[0].snapshot.run["run_id"] == "selected"
    assert result.items[0].opencode is not None
    messages = result.items[0].opencode.messages
    assert [value["id"] for value in messages] == ["turn"]
    assert messages[0]["parts"][0]["completion"] == "unknown"
    assert "end" not in messages[0]["parts"][0]


def test_github_does_not_merge_later_body_comment_or_diff(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": {**github_base(), "title": "selected", "body": "old body"},
            "pull-request.json": {"node_id": "PR_1"},
            "comments.001.json": [
                {"id": 1, "body": "old comment", "created_at": "2026-01-01T01:00:00Z"}
            ],
            "pull-request.diff": "old diff\n",
        },
        run_id="selected",
        from_text="2025-12-28T00:00:00+00:00",
        to_text="2025-12-29T00:00:00+00:00",
        observation_window={
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T17:00:00Z",
        },
    )
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": {**github_base(), "title": "later", "body": "later body"},
            "pull-request.json": {"node_id": "PR_1"},
            "comments.001.json": [
                {"id": 2, "body": "later comment", "created_at": "2026-01-01T02:00:00Z"}
            ],
            "pull-request.diff": "later diff\n",
        },
        run_id="later",
        from_text="2025-12-29T00:00:00+00:00",
        to_text="2025-12-30T00:00:00+00:00",
        observation_window={
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T18:00:00Z",
        },
    )

    write_github_profile(archive)
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    overview = text(output, "overview.md")
    activity = text(output, "activity.md")
    diff = text(output, "diff.patch")
    assert "selected" in overview and "old body" in overview
    assert "# example/project PR #1 — later" not in overview
    assert "later body" not in overview
    assert "Mutable fields may include" in overview
    assert "old comment" in activity and "later comment" not in activity
    assert diff == "old diff\n"


def test_opencode_groups_by_project_worktree_and_shows_distinct_workdirs(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    project = {"id": "hidden", "worktree": "/repo"}
    publish_opencode(
        archive,
        session(
            "one",
            directory="/repo/.worktrees/one",
            messages=[message("one-message", "2026-01-01T01:00:00Z")],
        ),
        "one-run",
        project=project,
    )
    publish_opencode(
        archive,
        session(
            "two",
            directory="/repo/.worktrees/two",
            messages=[message("two-message", "2026-01-01T02:00:00Z")],
        ),
        "two-run",
        project=project,
        from_text="2026-01-02T00:00:00+08:00",
        to_text="2026-01-03T00:00:00+08:00",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    index = (output / "index.md").read_text()
    assert index.count("### `/repo`") == 1
    overviews = "\n".join(path.read_text() for path in output.rglob("overview.md"))
    assert "/repo/.worktrees/one" in overviews
    assert "/repo/.worktrees/two" in overviews
    assert "Project ID" not in index
    assert "hidden" not in index


def test_opencode_global_project_falls_back_to_each_session_directory(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    global_project = {"id": "global", "worktree": "/"}
    publish_opencode(
        archive,
        session(
            "global-one",
            directory="/projects/one",
            messages=[message("one-message", "2026-01-01T01:00:00Z")],
        ),
        "global-one-run",
        project=global_project,
    )
    publish_opencode(
        archive,
        session(
            "global-two",
            directory="/projects/two",
            messages=[message("two-message", "2026-01-01T02:00:00Z")],
        ),
        "global-two-run",
        project=global_project,
        from_text="2026-01-02T00:00:00+08:00",
        to_text="2026-01-03T00:00:00+08:00",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    index = (output / "index.md").read_text()

    assert "### `/projects/one`" in index
    assert "### `/projects/two`" in index
    assert "### `/`" not in index
    overviews = "\n".join(path.read_text() for path in output.rglob("overview.md"))
    assert "Project directory: `/projects/one`" in overviews
    assert "Project directory: `/projects/two`" in overviews


def test_opencode_without_project_json_falls_back_to_session_directory(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "legacy",
            directory="/legacy/project",
            messages=[message("legacy-message", "2026-01-01T01:00:00Z")],
        ),
        "legacy-run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    index = (output / "index.md").read_text()
    overview = text(output, "overview.md")
    assert "### `/legacy/project`" in index
    assert "Project directory: `/legacy/project`" in overview
    assert "Working directory:" not in overview


def test_opencode_context_output_is_root_session_only(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T01:00:00Z")]),
        "root-run",
    )
    publish_opencode(
        archive,
        session(
            "child", parent_id="root", messages=[message("c", "2026-01-01T01:01:00Z")]
        ),
        "child-run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert "child" not in (output / "index.md").read_text()
    assert len(list(output.rglob("overview.md"))) == 1


def test_opencode_keeps_text_and_drops_task_question_and_other_tools(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {"type": "text", "text": "kept user text"},
        {"type": "tool", "tool": "task"},
        {"type": "tool", "tool": "question"},
        {"type": "tool", "tool": "bash"},
        {"type": "tool", "tool": "edit"},
        {"type": "tool", "tool": "write"},
        {"type": "tool", "tool": "unknown"},
    ]
    activity = opencode_activity(archive, tmp_path, parts, "2026-01-01T00:30:00Z")
    assert "kept user text" in activity
    assert "Delegated task" not in activity
    assert "Question" not in activity
    assert "bash" not in activity
    assert "unknown" not in activity


def test_opencode_tool_only_background_has_no_rendered_document(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    task = {
        "type": "tool",
        "tool": "task",
        "state": {
            "status": "completed",
            "time": {
                "start": "2026-01-01T15:59:00Z",
                "end": "2026-01-01T16:01:00Z",
            },
            "output": "future result",
        },
    }
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T15:00:00Z", [task])]),
        "run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert not list(output.rglob("activity.md"))


def test_unknown_task_end_is_not_active_on_later_request_day(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    task = {
        "type": "tool",
        "tool": "task",
        "state": {
            "status": "running",
            "time": {"start": "2026-01-01T15:00:00Z"},
        },
    }
    publish_opencode(
        archive,
        session(
            "root",
            messages=[message("m", "2026-01-01T15:00:00Z", [task])],
        ),
        "unknown-run",
    )

    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
        output,
    )
    assert not list(output.rglob("activity.md"))


def test_historical_tool_outcomes_are_not_rendered(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T10:00:00Z",
                    "end": "2026-01-01T11:00:00Z",
                },
                "output": "historical result",
            },
        },
        {
            "type": "tool",
            "tool": "question",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T10:00:00Z",
                    "end": "2026-01-01T12:00:00Z",
                },
                "input": {"questions": ["Continue?"]},
                "metadata": {"answers": ["yes"]},
            },
        },
        {
            "type": "tool",
            "tool": "read",
            "state": {
                "status": "error",
                "time": {
                    "start": "2026-01-01T10:00:00Z",
                    "end": "2026-01-01T13:00:00Z",
                },
                "error": "historical error",
            },
        },
        {
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T10:00:00Z",
                    "end": "2026-01-01T17:00:00Z",
                },
                "output": "post-cutoff result",
            },
        },
    ]
    publish_opencode(
        archive,
        session("root", messages=[message("m", "2026-01-01T10:00:00Z", parts)]),
        "historical-run",
        from_text="2026-01-02T00:00:00+08:00",
        to_text="2026-01-03T00:00:00+08:00",
    )

    output = tmp_path / "output"
    generate_context(
        archive.root,
        ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-01T16:00:00Z"),
        output,
    )
    assert not list(output.rglob("activity.md"))


def test_opencode_cutoff_hides_later_text_when_tools_are_removed(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {"type": "text", "text": "retained text"},
        {
            "type": "text",
            "text": "future text",
            "time": {"created": "2026-01-01T16:00:00Z"},
        },
        {
            "type": "tool",
            "tool": "task",
            "state": {
                "status": "error",
                "time": {
                    "start": "2026-01-01T15:00:00Z",
                    "end": "2026-01-01T16:01:00Z",
                },
                "output": "future task result",
                "error": "future task error",
            },
        },
        {
            "type": "tool",
            "tool": "question",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T15:30:00Z",
                    "end": "2026-01-01T16:01:00Z",
                },
                "metadata": {"answers": ["future answer"]},
                "input": {"questions": ["Continue?"]},
            },
        },
    ]
    activity = opencode_activity(archive, tmp_path, parts, "2026-01-01T14:00:00Z")

    assert "retained text" in activity
    assert "future text" not in activity
    assert "future task result" not in activity
    assert "future task error" not in activity
    assert "future answer" not in activity


def test_opencode_in_range_tool_keeps_earlier_text_in_background(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    tool = {
        "type": "tool",
        "tool": "bash",
        "state": {
            "status": "completed",
            "time": {
                "start": "2025-12-31T17:00:00Z",
                "end": "2025-12-31T17:01:00Z",
            },
        },
    }
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "earlier-text",
                    "2025-12-31T10:00:00Z",
                    [{"type": "text", "text": "earlier text"}, tool],
                )
            ],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert "earlier text" in text(output, "background.md")
    assert not list(output.rglob("activity.md"))
    assert "01:00" in (output / "index.md").read_text()


def test_opencode_file_and_shell_tools_are_not_rendered(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    parts = [
        {"type": "text", "text": "text remains"},
        {
            "type": "tool",
            "tool": "apply_patch",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:00:00Z",
                    "end": "2026-01-01T01:01:00Z",
                },
                "input": {
                    "patchText": "*** Update File: src/foo.py\n@@ ignored hunk\n"
                },
            },
        },
        {
            "type": "tool",
            "tool": "edit",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:02:00Z",
                    "end": "2026-01-01T01:03:00Z",
                },
                "input": {
                    "filePath": "/home/tester/dev/example/project/src/foo.py",
                    "oldString": "old secret",
                    "newString": "new secret",
                },
            },
        },
        {
            "type": "tool",
            "tool": "write",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:04:00Z",
                    "end": "2026-01-01T01:05:00Z",
                },
                "input": {"filePath": "src/bar.py", "content": "body secret"},
            },
        },
        {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:06:00Z",
                    "end": "2026-01-01T01:07:00Z",
                },
                "input": {
                    "command": "pytest",
                    "timeout": 30,
                    "workdir": "/tmp/elsewhere",
                },
            },
        },
        {
            "type": "tool",
            "tool": "grep",
            "state": {
                "status": "completed",
                "time": {
                    "start": "2026-01-01T01:08:00Z",
                    "end": "2026-01-01T01:09:00Z",
                },
                "input": {"pattern": "hidden"},
            },
        },
    ]
    activity = opencode_activity(archive, tmp_path, parts, "2026-01-01T00:30:00Z")
    assert "text remains" in activity
    assert "src/foo.py" not in activity and "src/bar.py" not in activity
    assert "pytest" not in activity
    assert "/tmp/elsewhere" not in activity
    assert "hidden" not in activity


def test_opencode_equal_workdir_is_omitted_and_relative_paths_are_normalized(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    part = {
        "type": "tool",
        "tool": "bash",
        "state": {
            "status": "completed",
            "time": {"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T01:01:00Z"},
            "input": {"command": "pwd", "workdir": "/home/tester/dev/example/project"},
        },
    }
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "m",
                    "2026-01-01T00:30:00Z",
                    [{"type": "text", "text": "text remains"}, part],
                )
            ],
        ),
        "run",
    )
    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    activity = text(output, "activity.md")
    assert "text remains" in activity
    assert "Workdir" not in activity


def test_opencode_background_window_keeps_latest_three_users_and_assistants(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    messages: list[dict[str, object]] = []
    for index in range(1, 5):
        append_user_assistant_turn(messages, index)
        if index == 2:
            messages.extend(
                [
                    message(
                        "tool-only-user",
                        "2025-12-31T02:45:00Z",
                        [
                            {
                                "type": "tool",
                                "tool": "bash",
                                "state": {
                                    "status": "completed",
                                    "time": {
                                        "start": "2025-12-31T02:45:00Z",
                                        "end": "2025-12-31T02:46:00Z",
                                    },
                                },
                            }
                        ],
                    ),
                    message("empty-user", "2025-12-31T02:47:00Z"),
                ]
            )
    messages.append(
        message(
            "current",
            "2026-01-01T01:00:00Z",
            [{"type": "text", "text": "current work"}],
        )
    )
    background = opencode_background(archive, tmp_path, messages)
    assert "user-1" not in background
    assert "assistant-1" not in background
    for index in (2, 3, 4):
        assert f"user-{index}" in background
        assert f"assistant-{index}" in background


def test_opencode_background_without_user_turns_is_retained(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "assistant-only",
                    "2025-12-31T01:00:00Z",
                    [{"type": "text", "text": "assistant-only background"}],
                    role="assistant",
                ),
                message(
                    "current",
                    "2026-01-01T01:00:00Z",
                    [{"type": "text", "text": "current work"}],
                ),
            ],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert "assistant-only background" in text(output, "background.md")


@pytest.mark.parametrize("user_count", [1, 3, 4])
def test_opencode_background_window_starts_at_latest_user_window(
    tmp_path: Path, user_count: int
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    messages: list[dict[str, object]] = [
        message(
            "assistant-prefix",
            "2025-12-31T00:30:00Z",
            [{"type": "text", "text": "assistant-only prefix"}],
            role="assistant",
        )
    ]
    for index in range(1, user_count + 1):
        append_user_assistant_turn(messages, index)
    messages.append(
        message(
            "current",
            "2026-01-01T01:00:00Z",
            [{"type": "text", "text": "current work"}],
        )
    )
    background = opencode_background(archive, tmp_path, messages)
    assert "assistant-only prefix" not in background
    first_retained = max(1, user_count - 2)
    for index in range(1, user_count + 1):
        if index < first_retained:
            assert f"user-{index}" not in background
            assert f"assistant-{index}" not in background
        else:
            assert f"user-{index}" in background
            assert f"assistant-{index}" in background


def test_opencode_repeated_long_user_text_remains_in_each_output(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    repeated = ("Repeated context paragraph. " * 9) + "Repeated context paragraph."
    publish_user_text_pair(archive, repeated)

    one, two = tmp_path / "one", tmp_path / "two"
    generate_context(archive.root, request(), one)
    generate_context(archive.root, request(), two)
    assert not list(one.rglob("repeated-user-text.md"))
    assert repeated in text(one, "activity.md")
    assert repeated in text(one, "background.md")
    assert_outputs_equal(one, two)


def test_opencode_repeated_user_text_is_rendered_without_shared_file(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    repeated = "short repeated text"
    publish_user_text_pair(archive, repeated)

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert repeated in text(output, "activity.md")
    assert repeated in text(output, "background.md")


def test_opencode_filtered_user_text_is_not_rendered_in_background(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    repeated = "filtered user text. " * 12
    messages = [
        message(
            "filtered-old",
            "2025-12-31T01:00:00Z",
            [{"type": "text", "text": repeated}],
        )
    ]
    messages.extend(
        [
            message(
                f"background-{index}",
                f"2025-12-31T0{index}:00:00Z",
                [{"type": "text", "text": f"background-{index}"}],
            )
            for index in range(2, 5)
        ]
    )
    messages.append(
        message(
            "current",
            "2026-01-01T01:00:00Z",
            [{"type": "text", "text": repeated}],
        )
    )
    publish_opencode(archive, session("root", messages=messages), "run")

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    assert repeated in text(output, "activity.md")
    assert repeated not in text(output, "background.md")


def test_opencode_user_markdown_is_not_reparsed_or_stripped(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    special = (
        "**User**\n\n"
        "```markdown\n"
        "**Assistant**\n\n"
        "fenced line with trailing spaces  \n"
        "```\n\n"
        "ordinary paragraph with trailing spaces  \n"
        + ("long content " * 20)
        + "final trailing spaces  "
    )
    publish_opencode(
        archive,
        session(
            "root",
            messages=[
                message(
                    "current",
                    "2026-01-01T01:00:00Z",
                    [{"type": "text", "text": special}],
                )
            ],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    activity = next(output.rglob("activity.md")).read_bytes()
    assert activity.count(special.encode("utf-8")) == 1


def test_deterministic_output_and_raw_archive_unchanged(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive, session("root", messages=[message("m", "2026-01-01T01:00:00Z")]), "run"
    )
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive.root.rglob("*")
        if path.is_file()
    }
    one, two = tmp_path / "one", tmp_path / "two"
    generate_context(archive.root, request(), one)
    generate_context(archive.root, request(), two)
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive.root.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert_outputs_equal(one, two)


def test_publication_errors_leave_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    original = Path.rename
    monkeypatch.setattr(Path, "rename", lambda *_args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ContextError, match="publication failed"):
        generate_context(archive.root, request(), tmp_path / "output")
    assert not (tmp_path / "output").exists()
    monkeypatch.setattr(Path, "rename", original)


def test_context_generation_is_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    generate_context(archive, request(), tmp_path / "output")


def test_source_attribution_mapping_is_explicit_and_rejects_unknown_kinds() -> None:
    assert source_attribution_mode("opencode") is AttributionMode.PERSONAL
    assert source_attribution_mode("chatgpt") is AttributionMode.PERSONAL
    assert source_attribution_mode("github") is AttributionMode.ACTOR_SCOPED
    with pytest.raises(AttributionError, match="no attribution semantics"):
        source_attribution_mode("future-source")


def test_personal_opencode_delegated_work_is_user_work_and_exposes_mode(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            directory="/workspace/example",
            messages=[
                message(
                    "delegated",
                    "2026-01-01T01:00:00Z",
                    [
                        {
                            "type": "text",
                            "text": "Delegated investigation and implementation",
                        }
                    ],
                    role="assistant",
                ),
                message(
                    "validation",
                    "2026-01-01T02:00:00Z",
                    [{"type": "text", "text": "Tests and validation complete"}],
                ),
            ],
        ),
        "run",
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)

    index = (output / "index.md").read_text()
    overview = text(output, "overview.md")
    activity = text(output, "activity.md")
    assert "attribution mode: `personal`" in index
    assert "- Attribution mode: `personal`" in overview
    assert "Attribution mode: `personal`" in activity
    assert "delegated agent or subagent" in overview
    assert "Delegated investigation and implementation" in activity
    assert "Tests and validation complete" in activity


def test_actor_scoped_collaboration_separates_tracked_work_from_collaborators(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(archive, emails=["tracked@example.com"])
    output = github_output(
        archive,
        tmp_path,
        {
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "tracked concern",
                    "user": {"login": "Tracked-User"},
                    "created_at": "2026-01-01T01:00:00Z",
                },
                {
                    "id": 2,
                    "body": "other finding",
                    "user": {"login": "other-reviewer"},
                    "created_at": "2026-01-01T02:00:00Z",
                },
            ],
            "reviews.001.json": [
                {
                    "id": 3,
                    "body": "approved after review",
                    "user": {"login": "TRACKED-USER"},
                    "state": "APPROVED",
                    "submitted_at": "2026-01-01T03:00:00Z",
                },
                {
                    "id": 4,
                    "body": "separate finding",
                    "user": {"login": "other-reviewer"},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-01-01T04:00:00Z",
                },
            ],
            "timeline.001.json": [
                {
                    "id": 5,
                    "node_id": "COLLAB_COMMIT",
                    "event": "committed",
                    "author": {
                        "name": "collaborator",
                        "email": "collaborator@example.com",
                        "date": "2026-01-01T05:00:00Z",
                    },
                    "committer": {"date": "2026-01-01T05:00:00Z"},
                    "sha": "collab123456",
                    "message": "Collaborator implementation",
                },
                {
                    "id": 6,
                    "node_id": "USER_COMMIT",
                    "event": "committed",
                    "author": {
                        "name": "tracked-user",
                        "email": "tracked@example.com",
                        "date": "2026-01-01T06:00:00Z",
                    },
                    "committer": {"date": "2026-01-01T06:00:00Z"},
                    "sha": "user12345678",
                    "message": "Tracked follow-up",
                },
                {
                    "id": 7,
                    "node_id": "TRACKED_CLOSED",
                    "event": "closed",
                    "actor": {"login": "tracked-user"},
                    "created_at": "2026-01-01T06:30:00Z",
                },
            ],
        },
        effective_options={"actor_login": "tracked-user"},
    )

    activity = text(output, "activity.md")
    assert "Attribution mode: `actor_scoped`" in text(output, "overview.md")
    assert activity.count("## Commits") == 1
    assert "## User work" not in activity
    assert "## Context-only evidence" not in activity
    assert "Attribution mode: `actor_scoped`" in activity
    assert activity.index("tracked concern") < activity.index("other finding")
    assert activity.index("approved after review") < activity.index("separate finding")
    assert activity.index("Collaborator implementation") < activity.index(
        "Tracked follow-up"
    )
    assert "Closed by @tracked-user (tracked account) · [User work]" in activity
    assert activity.count("[User work]") == 4
    assert activity.count("[Context only]") == 4

    result = extract_context(request(), load_archive(archive.root), archive.root)
    projection = result.items[0].github
    assert projection is not None
    ids = github_user_work_record_ids(projection, request().start, request().end)
    assert ("ordinary-comment", "1") in ids
    assert ("review", "3") in ids
    assert ("timeline", "USER_COMMIT") in ids
    assert ("ordinary-comment", "2") not in ids
    assert ("review", "4") not in ids
    assert ("timeline", "COLLAB_COMMIT") not in ids


def test_actor_scoped_item_with_only_collaborator_activity_is_context_only(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    write_github_profile(archive)
    output = github_output(
        archive,
        tmp_path,
        {
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "collaborator-only discussion",
                    "user": {"login": "collaborator"},
                    "created_at": "2026-01-01T01:00:00Z",
                }
            ],
            "timeline.001.json": [
                {
                    "id": 2,
                    "node_id": "COLLAB_COMMIT",
                    "event": "committed",
                    "author": {"email": "collaborator@example.com"},
                    "committer": {
                        "date": "2026-01-01T02:00:00Z",
                    },
                    "sha": "collab123456",
                    "message": "Important collaborator implementation",
                }
            ],
        },
        effective_options={"actor_login": "tracked-user"},
    )

    overview = text(output, "overview.md")
    activity = text(output, "activity.md")
    assert "No tracked-account user work was identified" not in overview
    assert "## User work" not in activity
    assert "## Context-only evidence" not in activity
    assert "Important collaborator implementation" in activity
    assert "[Context only]" in activity
    assert "attribution mode: `actor_scoped`" in (output / "index.md").read_text()


def test_mixed_sources_keep_personal_work_and_tracked_actions_only(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_opencode(
        archive,
        session(
            "root",
            directory="/workspace/example",
            messages=[
                message(
                    "design",
                    "2026-01-01T01:00:00Z",
                    [{"type": "text", "text": "Delegated design decision"}],
                )
            ],
        ),
        "opencode-run",
    )
    write_github_profile(archive, emails=["tracked@example.com"])
    publish_github(
        archive,
        "PR_1",
        {
            "issue.json": github_base(),
            "pull-request.json": {"node_id": "PR_1"},
            "comments.001.json": [
                {
                    "id": 1,
                    "body": "tracked decision",
                    "user": {"login": "tracked-user"},
                    "created_at": "2026-01-01T02:00:00Z",
                },
                {
                    "id": 2,
                    "body": "collaborator implementation details",
                    "user": {"login": "collaborator"},
                    "created_at": "2026-01-01T03:00:00Z",
                },
            ],
        },
        effective_options={"actor_login": "tracked-user"},
    )

    output = tmp_path / "output"
    generate_context(archive.root, request(), output)
    index = (output / "index.md").read_text()
    assert index.count("attribution mode: `personal`") == 1
    assert index.count("attribution mode: `actor_scoped`") == 1
    activity_files = {path.read_text() for path in output.rglob("activity.md")}
    assert any("Delegated design decision" in content for content in activity_files)
    github_activity = next(
        content for content in activity_files if "tracked decision" in content
    )
    assert "tracked decision" in github_activity
    assert "collaborator implementation details" in github_activity
    assert "## User work" not in github_activity
    assert "## Context-only evidence" not in github_activity
    assert github_activity.index("[User work]") < github_activity.index(
        "tracked decision"
    )
    assert "[Context only]" in github_activity
