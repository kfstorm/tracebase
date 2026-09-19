"""Shared text-only dialogue projection and Markdown rendering."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

_BACKGROUND_USER_TURN_LIMIT = 3


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    role: str
    timestamp: datetime
    text: str


@dataclass(frozen=True, slots=True)
class DialogueTranscript:
    activity: tuple[DialogueTurn, ...]
    background: tuple[DialogueTurn, ...]


def dialogue_sort_key(
    transcript: DialogueTranscript, title: str, source_id: str
) -> tuple[datetime, str, str]:
    """Order conversational items by retained activity and stable metadata."""
    return min(turn.timestamp for turn in transcript.activity), title, source_id


def project_dialogue(
    turns: Iterable[DialogueTurn],
    start: datetime,
    end: datetime,
) -> DialogueTranscript | None:
    """Classify text turns and retain bounded earlier background.

    Source adapters are responsible for producing turns in source order. This
    function deliberately does not sort them: provider page and array order is
    part of the observed conversational stream.
    """
    activity: list[DialogueTurn] = []
    background: list[DialogueTurn] = []
    for turn in turns:
        if start <= turn.timestamp < end:
            activity.append(turn)
        elif turn.timestamp < start:
            background.append(turn)

    if not activity:
        return None

    user_positions = [
        index for index, turn in enumerate(background) if turn.role == "user"
    ]
    if user_positions:
        first_retained = user_positions[
            max(0, len(user_positions) - _BACKGROUND_USER_TURN_LIMIT)
        ]
        background = background[first_retained:]

    return DialogueTranscript(tuple(activity), tuple(background))


def format_timestamp(value: datetime, timezone: tzinfo) -> str:
    return value.astimezone(timezone).strftime("%Y-%m-%d %H:%M")


def render_dialogue(
    transcript: DialogueTranscript,
    bucket: str,
    timezone: tzinfo,
    attribution_mode: str,
) -> list[str]:
    """Render one transcript bucket using the shared conversational format."""
    turns = transcript.activity if bucket == "activity" else transcript.background
    if not turns:
        return []
    lines = [
        f"# {'Activity' if bucket == 'activity' else 'Background'}",
        "",
        f"Attribution mode: `{attribution_mode}`",
        "",
    ]
    for turn in turns:
        lines.extend(
            [
                "**"
                f"{turn.role.capitalize()} · "
                f"{format_timestamp(turn.timestamp, timezone)}**",
                "",
                turn.text,
                "",
            ]
        )
    return lines


def write_dialogue_markdown(path: Path, lines: list[str]) -> None:
    """Write dialogue without reparsing or stripping source text."""
    content_lines = lines[:-1] if lines and lines[-1] == "" else lines
    path.write_text("\n".join(content_lines) + "\n", encoding="utf-8")


def render_dialogue_files(
    transcript: DialogueTranscript,
    timezone: tzinfo,
    attribution_mode: str,
    overview: list[str],
) -> dict[str, list[str]]:
    files = {
        "overview.md": overview,
        "activity.md": render_dialogue(
            transcript, "activity", timezone, attribution_mode
        ),
    }
    background = render_dialogue(transcript, "background", timezone, attribution_mode)
    if background:
        files["background.md"] = background
    return files


__all__ = [
    "DialogueTranscript",
    "DialogueTurn",
    "dialogue_sort_key",
    "format_timestamp",
    "project_dialogue",
    "render_dialogue",
    "render_dialogue_files",
    "write_dialogue_markdown",
]
