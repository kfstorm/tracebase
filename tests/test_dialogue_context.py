import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracebase.archive import (
    Archive,
    ArchiveError,
    CollectionRange,
    CollectionRun,
    PublishedSnapshot,
    Snapshot,
)
from tracebase.chatgpt_context import project_chatgpt
from tracebase.context import (
    ContextRequest,
    extract_context,
    generate_context,
    load_archive,
)
from tracebase.dialogue import DialogueTurn, project_dialogue, render_dialogue
from tracebase.opencode_context import project_opencode

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)


def chat_message(
    message_id: str,
    timestamp: int,
    *,
    role: str = "user",
    text: str = "text",
    content_type: str = "text",
    update_time: int | None = None,
    parts: list[object] | None = None,
    hidden: bool = False,
) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if hidden:
        metadata["is_visually_hidden_from_conversation"] = True
    return {
        "id": message_id,
        "author": {"role": role},
        "create_time": timestamp,
        "update_time": update_time if update_time is not None else timestamp,
        "content": {
            "content_type": content_type,
            "parts": parts if parts is not None else [text],
        },
        "metadata": metadata,
    }


def chat_snapshot(
    detail_messages: list[dict[str, object]],
    older_pages: list[list[dict[str, object]]] | None = None,
    *,
    descriptors: list[tuple[str, str, int, str]] | None = None,
    current_node: str | None = None,
) -> PublishedSnapshot:
    older_pages = older_pages or []
    page_values: dict[str, dict[str, object]] = {
        "conversation.json": {
            "title": "Synthetic conversation",
            "current_node": current_node or str(detail_messages[-1]["id"]),
            "has_versions": True,
            "messages": detail_messages,
        }
    }
    page_descriptors = descriptors or [
        ("conversation.json", "detail", 1, "hydration-1")
    ]
    for index, messages in enumerate(older_pages, start=1):
        page_values[f"messages.{index:03d}.json"] = {"messages": messages}
    manifest_descriptors = [
        {
            "path": path,
            "request": {
                "hydration_id": hydration_id,
                "page_kind": kind,
                "page_order": order,
            },
        }
        for path, kind, order, hydration_id in page_descriptors
    ]
    return PublishedSnapshot(
        {},
        {
            "source_id": "conversation-1",
            "evidence_files": manifest_descriptors,
        },
        {
            path: json.dumps(page_values[path]).encode()
            for path, _kind, _order, _hydration in page_descriptors
        },
        Path("/synthetic/snapshot"),
    )


def opencode_snapshot(messages: list[dict[str, object]]) -> PublishedSnapshot:
    payload = {
        "id": "session-1",
        "info": {"id": "session-1", "title": "Synthetic session"},
        "messages": messages,
    }
    return PublishedSnapshot(
        {},
        {"source_id": "session-1"},
        {"session.json": json.dumps(payload).encode()},
        Path("/synthetic/snapshot"),
    )


def test_shared_dialogue_has_half_open_buckets_and_bounded_background() -> None:
    turns = [
        DialogueTurn("user", datetime(2025, 12, 1, tzinfo=UTC), "old-1"),
        DialogueTurn("assistant", datetime(2025, 12, 1, tzinfo=UTC), "old-1-a"),
        DialogueTurn("user", datetime(2025, 12, 2, tzinfo=UTC), "old-2"),
        DialogueTurn("user", START, "at-start"),
        DialogueTurn("assistant", END, "at-end"),
    ]

    transcript = project_dialogue(turns, START, END)

    assert transcript is not None
    assert [turn.text for turn in transcript.activity] == ["at-start"]
    assert [turn.text for turn in transcript.background] == [
        "old-1",
        "old-1-a",
        "old-2",
    ]
    assert "at-end" not in {turn.text for turn in transcript.activity}
    assert "at-end" not in {turn.text for turn in transcript.background}
    assert "**User · 2026-01-01 00:00**" in "\n".join(
        render_dialogue(transcript, "activity", UTC, "personal")
    )


def test_opencode_projects_text_only_and_uses_message_created_time() -> None:
    projection = project_opencode(
        opencode_snapshot(
            [
                {
                    "id": "earlier",
                    "role": "user",
                    "created": "2025-12-31T23:00:00Z",
                    "parts": [{"type": "text", "text": "background"}],
                },
                {
                    "id": "current",
                    "role": "assistant",
                    "created": "2026-01-01T01:00:00Z",
                    "parts": [
                        {"type": "text", "text": "visible"},
                        {"type": "tool", "tool": "bash"},
                        {"type": "code", "text": "not dialogue"},
                    ],
                },
                {
                    "id": "future",
                    "role": "user",
                    "created": "2026-01-02T01:00:00Z",
                    "parts": [{"type": "text", "text": "future"}],
                },
            ]
        ),
        START,
        END,
    )

    assert projection is not None
    assert [turn.text for turn in projection.dialogue.activity] == ["visible"]
    assert [turn.text for turn in projection.dialogue.background] == ["background"]


def test_opencode_tool_only_activity_does_not_select_session() -> None:
    assert (
        project_opencode(
            opencode_snapshot(
                [
                    {
                        "id": "tool",
                        "role": "assistant",
                        "created": "2026-01-01T01:00:00Z",
                        "parts": [{"type": "tool", "tool": "bash"}],
                    }
                ]
            ),
            START,
            END,
        )
        is None
    )


def test_chatgpt_reconstructs_oldest_to_detail_without_timestamp_sort() -> None:
    old = chat_message("old", 1767139200, text="old")
    middle_a = chat_message("middle-a", 1767229200, text="middle-a")
    middle_b = chat_message("middle-b", 1767225600, text="middle-b")
    latest = chat_message("latest", 1767229260, role="assistant", text="latest")
    latest["parent_id"] = "unavailable-branch-record"
    snapshot = chat_snapshot(
        [middle_a, middle_b, latest],
        [[old]],
        descriptors=[
            ("messages.001.json", "older-messages", 2, "hydration-1"),
            ("conversation.json", "detail", 1, "hydration-1"),
        ],
    )

    projection = project_chatgpt(snapshot, START, END)

    assert projection is not None
    assert [turn.text for turn in projection.dialogue.activity] == [
        "middle-a",
        "middle-b",
        "latest",
    ]
    assert [turn.text for turn in projection.dialogue.background] == ["old"]


def test_chatgpt_reconstructs_multiple_older_pages_by_manifest_order() -> None:
    oldest = chat_message("oldest", 1767139200, text="oldest")
    older = chat_message("older", 1767225600, text="older")
    latest = chat_message("latest", 1767229260, role="assistant", text="latest")
    snapshot = chat_snapshot(
        [latest],
        [[older], [oldest]],
        descriptors=[
            ("messages.002.json", "older-messages", 3, "hydration-1"),
            ("conversation.json", "detail", 1, "hydration-1"),
            ("messages.001.json", "older-messages", 2, "hydration-1"),
        ],
    )

    projection = project_chatgpt(snapshot, START, END)

    assert projection is not None
    assert [turn.text for turn in projection.dialogue.activity] == ["older", "latest"]
    assert [turn.text for turn in projection.dialogue.background] == ["oldest"]


def test_chatgpt_deduplicates_identical_message_payloads() -> None:
    duplicate = chat_message("same", 1767229200, text="same")
    projection = project_chatgpt(
        chat_snapshot(
            [duplicate],
            [[duplicate]],
            descriptors=[
                ("messages.001.json", "older-messages", 2, "hydration-1"),
                ("conversation.json", "detail", 1, "hydration-1"),
            ],
        ),
        START,
        END,
    )

    assert projection is not None
    assert [turn.text for turn in projection.dialogue.activity] == ["same"]


def test_chatgpt_rejects_conflicting_duplicate_message_payloads() -> None:
    detail = chat_message("same", 1767229200, text="new")
    older = chat_message("same", 1767229200, text="old")

    with pytest.raises(ArchiveError, match="duplicate message payload differs"):
        project_chatgpt(
            chat_snapshot(
                [detail],
                [[older]],
                descriptors=[
                    ("messages.001.json", "older-messages", 2, "hydration-1"),
                    ("conversation.json", "detail", 1, "hydration-1"),
                ],
            ),
            START,
            END,
        )


@pytest.mark.parametrize(
    "descriptors",
    [
        [("conversation.json", "detail", 0, "hydration-1")],
        [
            ("conversation.json", "detail", 1, "hydration-1"),
            ("messages.001.json", "older-messages", 2, "hydration-2"),
        ],
        [
            ("conversation.json", "detail", 2, "hydration-1"),
            ("messages.001.json", "older-messages", 1, "hydration-1"),
        ],
    ],
)
def test_chatgpt_rejects_malformed_page_provenance(
    descriptors: list[tuple[str, str, int, str]],
) -> None:
    detail = chat_message("current", 1767229200)
    with pytest.raises(ArchiveError, match="page metadata"):
        project_chatgpt(
            chat_snapshot(
                [detail],
                [[chat_message("old", 1767139200)]],
                descriptors=descriptors,
            ),
            START,
            END,
        )


def test_chatgpt_current_node_mismatch_fails_without_parent_traversal() -> None:
    with pytest.raises(ArchiveError, match="current_node"):
        project_chatgpt(
            chat_snapshot(
                [chat_message("current", 1767229200)],
                current_node="unrelated-parent",
            ),
            START,
            END,
        )


def test_chatgpt_filters_records_and_uses_create_time_not_update_time() -> None:
    messages = [
        chat_message("start", 1767225600, text="at start", update_time=1900000000),
        chat_message("cutoff", 1767312000, text="at cutoff", update_time=1767225600),
        chat_message("system", 1767229200, role="system", text="system"),
        chat_message("code", 1767229200, content_type="code", text="code"),
        chat_message("hidden", 1767229200, text="hidden", hidden=True),
        chat_message("mixed", 1767229200, parts=["safe", {"image": "x"}]),
        chat_message("empty", 1767229200, text="", parts=[""]),
        chat_message("current", 1767229260, role="assistant", text="assistant"),
    ]

    projection = project_chatgpt(chat_snapshot(messages), START, END)

    assert projection is not None
    assert [turn.text for turn in projection.dialogue.activity] == [
        "at start",
        "assistant",
    ]
    assert all("update_time" not in turn.text for turn in projection.dialogue.activity)


def test_chatgpt_with_no_in_range_text_is_not_projected() -> None:
    later = chat_message("later", 1767312000, text="later")
    assert project_chatgpt(chat_snapshot([later]), START, END) is None


def publish_chatgpt_batch(
    archive: Archive,
    conversations: dict[str, list[dict[str, object]]],
    *,
    run_id: str,
    collection_from: str = "2025-12-01T00:00:00Z",
    collection_to: str = "2025-12-02T00:00:00Z",
    observation_window: dict[str, str] | None = None,
) -> None:
    collection_range = CollectionRange.parse(collection_from, collection_to)
    run = CollectionRun(
        archive,
        "chatgpt",
        "account-1",
        collection_range,
        "test",
        {},
        run_id=run_id,
    )
    for source_id, messages in conversations.items():
        snapshot = run.write_snapshot(
            Snapshot(
                "chatgpt",
                "conversation",
                source_id,
                observation_window or collection_range.as_manifest(),
                (
                    {
                        "path": "conversation.json",
                        "request": {
                            "hydration_id": f"hydration-{source_id}",
                            "page_kind": "detail",
                            "page_order": 1,
                        },
                    },
                ),
            )
        )
        run.write_evidence(
            snapshot,
            "conversation.json",
            json.dumps(
                {
                    "title": f"Synthetic {source_id}",
                    "current_node": messages[-1]["id"],
                    "messages": messages,
                }
            ).encode(),
        )
    run.publish({"selected_conversation_count": len(conversations)})


def test_chatgpt_multiple_conversations_are_deterministic_and_mixed_source_safe(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_chatgpt_batch(
        archive,
        {
            "conversation-a": [chat_message("a", 1767229200, text="a")],
            "conversation-b": [chat_message("b", 1767229260, text="b")],
        },
        run_id="chatgpt-run",
    )
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")

    one, two = tmp_path / "one", tmp_path / "two"
    generate_context(archive.root, request, one)
    generate_context(archive.root, request, two)

    assert {
        item.snapshot.manifest["source_id"]
        for item in extract_context(request, load_archive(archive.root)).items
    } == {"conversation-a", "conversation-b"}
    assert "## ChatGPT" in (one / "index.md").read_text()
    assert sorted(
        path.relative_to(one).as_posix() for path in one.rglob("*.md")
    ) == sorted(path.relative_to(two).as_posix() for path in two.rglob("*.md"))
    assert [path.read_bytes() for path in sorted(one.rglob("*.md"))] == [
        path.read_bytes() for path in sorted(two.rglob("*.md"))
    ]


def test_chatgpt_non_text_only_conversation_has_no_context_item_or_files(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_chatgpt_batch(
        archive,
        {"conversation-tool": [chat_message("tool", 1767229200, content_type="code")]},
        run_id="chatgpt-run",
    )
    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    output = tmp_path / "output"

    result = extract_context(request, load_archive(archive.root))
    generate_context(archive.root, request, output)

    assert result.items == ()
    assert {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    } == {"index.md"}


def test_chatgpt_later_snapshot_supplies_earlier_messages(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    publish_chatgpt_batch(
        archive,
        {"conversation-1": [chat_message("old", 1767229200, text="old")]},
        run_id="old-run",
        observation_window={
            "from": "2026-01-01T09:00:00Z",
            "to": "2026-01-01T10:00:00Z",
        },
    )
    publish_chatgpt_batch(
        archive,
        {
            "conversation-1": [
                chat_message("old", 1767229200, text="old"),
                chat_message("current", 1767232800, text="current"),
            ]
        },
        run_id="later-run",
        collection_from="2025-12-02T00:00:00Z",
        collection_to="2025-12-03T00:00:00Z",
        observation_window={
            "from": "2026-01-02T09:00:00Z",
            "to": "2026-01-02T10:00:00Z",
        },
    )

    request = ContextRequest.parse("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    result = extract_context(request, load_archive(archive.root))

    assert result.items[0].snapshot.run["run_id"] == "later-run"
    assert result.items[0].chatgpt is not None
    assert [turn.text for turn in result.items[0].chatgpt.dialogue.activity] == [
        "old",
        "current",
    ]
