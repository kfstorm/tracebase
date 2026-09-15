"""Explicit Context adapter composition."""

from __future__ import annotations

from .chatgpt_context import CHATGPT_CONTEXT_ADAPTER
from .context_adapter import ContextAdapter
from .github_context import GITHUB_CONTEXT_ADAPTER
from .opencode_context import OPENCODE_CONTEXT_ADAPTER

CONTEXT_ADAPTERS: tuple[ContextAdapter, ...] = (
    GITHUB_CONTEXT_ADAPTER,
    OPENCODE_CONTEXT_ADAPTER,
    CHATGPT_CONTEXT_ADAPTER,
)


def adapter_for(source_kind: str) -> ContextAdapter | None:
    """Find a statically wired adapter without a dynamic plugin mechanism."""
    return next(
        (adapter for adapter in CONTEXT_ADAPTERS if adapter.source_kind == source_kind),
        None,
    )
