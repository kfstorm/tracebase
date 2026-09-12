"""Metrics derived from exported OpenCode sessions and persisted accounting."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import fsum
from typing import Any

from .summary_sessions import SessionRecord, iter_parts


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    session_count: int
    model_calls: int
    tool_calls: dict[str, int]
    tokens: dict[str, int]
    opencode_recorded_cost: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_count": self.session_count,
            "model_calls": self.model_calls,
            "tool_calls": dict(sorted(self.tool_calls.items())),
            "tokens": self.tokens,
            "opencode_recorded_cost": self.opencode_recorded_cost,
        }


def _fallback_tokens(export: dict[str, Any]) -> dict[str, int]:
    info = export.get("info")
    tokens = info.get("tokens") if isinstance(info, dict) else None
    if not isinstance(tokens, dict):
        return {}
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    return {
        key: value
        for key, value in (
            ("input", tokens.get("input")),
            ("output", tokens.get("output")),
            ("reasoning", tokens.get("reasoning")),
            ("cache_read", tokens.get("cache_read", cache.get("read"))),
            ("cache_write", tokens.get("cache_write", cache.get("write"))),
        )
        if isinstance(value, int) and not isinstance(value, bool)
    }


def aggregate_metrics(
    session_ids: tuple[str, ...],
    records: dict[str, SessionRecord],
    exports: dict[str, dict[str, Any]],
) -> UsageMetrics:
    """Sum each persisted session exactly once and count parts from exports."""
    token_keys = (
        ("input", "tokens_input"),
        ("output", "tokens_output"),
        ("reasoning", "tokens_reasoning"),
        ("cache_read", "tokens_cache_read"),
        ("cache_write", "tokens_cache_write"),
    )
    tokens = {key: 0 for key, _ in token_keys}
    costs: list[float] = []
    model_calls = 0
    tool_calls: Counter[str] = Counter()
    for session_id in session_ids:
        record = records.get(session_id)
        fallback = _fallback_tokens(exports.get(session_id, {}))
        if record is not None:
            for output_key, record_key in token_keys:
                value = getattr(record, record_key)
                tokens[output_key] += value if value else fallback.get(output_key, 0)
            costs.append(record.cost)
        else:
            for key in tokens:
                tokens[key] += fallback.get(key, 0)
        for part in iter_parts(exports.get(session_id, {})):
            if part.get("type") == "step-finish":
                model_calls += 1
            if part.get("type") == "tool":
                name = part.get("tool")
                tool_calls[name if isinstance(name, str) and name else "unknown"] += 1
    return UsageMetrics(
        session_count=len(session_ids),
        model_calls=model_calls,
        tool_calls=dict(tool_calls),
        tokens=tokens,
        opencode_recorded_cost=fsum(costs),
    )
