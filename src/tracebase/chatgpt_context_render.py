"""Consumer-oriented ChatGPT Context Output rendering."""

from __future__ import annotations

from pathlib import Path

from .chatgpt_context import ChatGPTProjection
from .context import ContextExtractionResult, ContextItem
from .context_adapter import context_semantics_lines
from .dialogue import render_dialogue_files, write_dialogue_markdown


def render_chatgpt(
    item: ContextItem, result: ContextExtractionResult, output: Path
) -> list[str]:
    projection = item.projection
    assert isinstance(projection, ChatGPTProjection)
    timezone = result.request.start.tzinfo
    assert timezone is not None
    overview = [f"# {projection.title}", ""]
    overview.extend(context_semantics_lines(item.adapter))
    files = render_dialogue_files(
        projection.dialogue,
        timezone,
        overview,
    )
    for name, lines in files.items():
        write_dialogue_markdown(output / name, lines)
    return list(files)


__all__ = ["render_chatgpt"]
