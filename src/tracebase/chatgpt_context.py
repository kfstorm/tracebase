"""Offline ChatGPT Snapshot reconstruction and Context projection."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive import ArchiveError, PublishedRun, PublishedSnapshot, encode_path_id
from .attribution import source_attribution_mode
from .context_adapter import (
    ContextIndexEntry,
    RenderedContextItem,
    render_source_item,
)
from .dialogue import DialogueTranscript, DialogueTurn, project_dialogue

if TYPE_CHECKING:
    from .context import ContextExtractionResult, ContextItem


@dataclass(frozen=True, slots=True)
class ChatGPTProjection:
    """ChatGPT display metadata plus the observed current text stream."""

    title: str
    dialogue: DialogueTranscript


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ArchiveError(f"ChatGPT {label} is invalid") from None
    if not isinstance(value, dict):
        raise ArchiveError(f"ChatGPT {label} is invalid")
    return value


def _page_descriptors(snapshot: PublishedSnapshot) -> list[tuple[int, str, bytes]]:
    declared = snapshot.manifest.get("evidence_files")
    if not isinstance(declared, list):
        raise ArchiveError("ChatGPT Snapshot page metadata is invalid")
    pages: list[tuple[int, str, bytes]] = []
    hydration_ids: set[str] = set()
    page_orders: set[int] = set()
    for descriptor in declared:
        if not isinstance(descriptor, dict):
            raise ArchiveError("ChatGPT Snapshot page metadata is invalid")
        path = descriptor.get("path")
        request = descriptor.get("request")
        if not isinstance(path, str) or not isinstance(request, dict):
            raise ArchiveError("ChatGPT Snapshot page metadata is invalid")
        hydration_id = request.get("hydration_id")
        page_kind = request.get("page_kind")
        page_order = request.get("page_order")
        if (
            not isinstance(hydration_id, str)
            or not hydration_id
            or not isinstance(page_kind, str)
            or page_kind not in {"detail", "older-messages"}
            or isinstance(page_order, bool)
            or not isinstance(page_order, int)
            or page_order < 1
        ):
            raise ArchiveError("ChatGPT Snapshot page metadata is invalid")
        if hydration_ids and hydration_id not in hydration_ids:
            raise ArchiveError("ChatGPT Snapshot page metadata is inconsistent")
        if page_order in page_orders:
            raise ArchiveError("ChatGPT Snapshot page metadata is invalid")
        hydration_ids.add(hydration_id)
        page_orders.add(page_order)
        try:
            raw = snapshot.evidence[path]
        except KeyError:
            raise ArchiveError("ChatGPT Snapshot page evidence is missing") from None
        pages.append((page_order, page_kind, raw))

    if not pages or sorted(page_orders) != list(range(1, len(pages) + 1)):
        raise ArchiveError("ChatGPT Snapshot page metadata is invalid")
    details = [page for page in pages if page[1] == "detail"]
    if len(details) != 1 or details[0][0] != 1:
        raise ArchiveError("ChatGPT Snapshot detail page metadata is invalid")
    if any(page[0] == 1 and page[1] != "detail" for page in pages):
        raise ArchiveError("ChatGPT Snapshot detail page metadata is invalid")
    # The collector records detail as page 1 and walks backward with increasing
    # page_order. Descending page_order reconstructs oldest through latest.
    return sorted(pages, key=lambda page: page[0], reverse=True)


def _message_id(message: dict[str, Any]) -> str:
    identifier = message.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ArchiveError("ChatGPT message payload is invalid")
    return identifier


def _reconstruct_messages(
    snapshot: PublishedSnapshot,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    pages = _page_descriptors(snapshot)
    detail = next(raw for order, kind, raw in pages if kind == "detail")
    detail_value = _json_object(detail, "conversation response")
    current_node = detail_value.get("current_node")
    raw_detail_messages = detail_value.get("messages")
    if (
        not isinstance(current_node, str)
        or not current_node
        or not isinstance(raw_detail_messages, list)
        or not raw_detail_messages
        or not isinstance(raw_detail_messages[-1], dict)
        or raw_detail_messages[-1].get("id") != current_node
    ):
        raise ArchiveError("ChatGPT current_node does not match detail stream")

    messages: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for _order, _kind, raw in pages:
        value = _json_object(raw, "conversation page")
        page_messages = value.get("messages")
        if not isinstance(page_messages, list):
            raise ArchiveError("ChatGPT conversation page is invalid")
        for message in page_messages:
            if not isinstance(message, dict):
                raise ArchiveError("ChatGPT message payload is invalid")
            identifier = _message_id(message)
            previous = seen.get(identifier)
            if previous is not None:
                if previous != message:
                    raise ArchiveError("ChatGPT duplicate message payload differs")
                continue
            seen[identifier] = message
            messages.append(message)
    return detail_value, tuple(messages)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArchiveError("ChatGPT message timestamp is invalid")
    if not math.isfinite(value):
        raise ArchiveError("ChatGPT message timestamp is invalid")
    try:
        return datetime.fromtimestamp(value, UTC)
    except OverflowError, OSError, ValueError:
        raise ArchiveError("ChatGPT message timestamp is invalid") from None


def _hidden(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    return bool(
        message.get("hidden") is True
        or (isinstance(metadata, dict) and metadata.get("hidden") is True)
        or (
            isinstance(metadata, dict)
            and metadata.get("is_visually_hidden_from_conversation") is True
        )
    )


def _text(message: dict[str, Any]) -> tuple[str, str, datetime] | None:
    author = message.get("author")
    role = author.get("role") if isinstance(author, dict) else None
    if role not in {"user", "assistant"} or _hidden(message):
        return None
    content = message.get("content")
    if not isinstance(content, dict) or content.get("content_type") != "text":
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
        # Mixed content is not safely reducible to plain text, so skip the
        # complete provider message rather than silently dropping a fragment.
        return None
    text = "\n\n".join(parts)
    if not text:
        return None
    return role, text, _timestamp(message.get("create_time"))


def project_chatgpt(
    snapshot: PublishedSnapshot, start: datetime, end: datetime
) -> ChatGPTProjection | None:
    """Project the provider-returned current stream without branch recovery."""
    detail, messages = _reconstruct_messages(snapshot)
    turns: list[DialogueTurn] = []
    for message in messages:
        turn = _text(message)
        if turn is None:
            continue
        role, text, timestamp = turn
        turns.append(DialogueTurn(role, timestamp, text))
    dialogue = project_dialogue(turns, start, end)
    if dialogue is None:
        return None
    title = detail.get("title")
    return ChatGPTProjection(
        title if isinstance(title, str) and title else "Untitled conversation",
        dialogue,
    )


class ChatGPTContextAdapter:
    source_kind = "chatgpt"
    object_kinds = frozenset({"conversation"})
    attribution_mode = source_attribution_mode("chatgpt")
    index_section = "ChatGPT"
    empty_index_message = "No ChatGPT conversations are available."

    def project(
        self, snapshot: PublishedSnapshot, start: datetime, end: datetime
    ) -> ChatGPTProjection | None:
        return project_chatgpt(snapshot, start, end)

    def include(self, projection: object) -> bool:
        if not isinstance(projection, ChatGPTProjection):
            raise ArchiveError("ChatGPT context projection was invalid")
        return True

    def prepare(
        self,
        items: tuple[ContextItem, ...],
        _runs: tuple[PublishedRun, ...],
        _archive_root: str | Path | None,
    ) -> tuple[ContextItem, ...]:
        return tuple(
            replace(
                item,
                path=(
                    "chatgpt/conversation/"
                    f"{encode_path_id(item.snapshot.manifest['source_id'])}"
                ),
            )
            for item in items
        )

    def render(
        self, item: ContextItem, result: ContextExtractionResult, output: Path
    ) -> RenderedContextItem:
        from .chatgpt_context_render import render_chatgpt  # noqa: PLC0415

        return render_source_item(self, item, result, output, render_chatgpt)

    def index_header(self, _items: tuple[ContextItem, ...]) -> tuple[str, ...]:
        return ()

    def index_metadata(
        self, item: ContextItem, _result: ContextExtractionResult
    ) -> ContextIndexEntry:
        projection = item.projection
        if not isinstance(projection, ChatGPTProjection):
            raise ArchiveError("ChatGPT context projection was invalid")
        return ContextIndexEntry(
            None,
            (projection.title, item.snapshot.manifest["source_id"]),
            projection.title,
        )


CHATGPT_CONTEXT_ADAPTER = ChatGPTContextAdapter()


__all__ = ["ChatGPTProjection", "project_chatgpt"]
