"""Small cooperative audit for obvious evidence-protocol violations."""

from __future__ import annotations

import re
from typing import Any

from .summary_sessions import iter_parts

_EXTERNAL_COMMAND = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:curl|wget|gh|ssh|scp)(?=\s|$|[;&|])",
    re.IGNORECASE,
)


def _bash_command(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("command", "cmd"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def audit_exports(exports: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for session_id, export in exports.items():
        for part in iter_parts(export):
            if part.get("type") != "tool":
                continue
            tool = part.get("tool")
            tool_name = tool if isinstance(tool, str) else "unknown"
            lower_tool = tool_name.lower()
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            tool_input = state.get("input")
            rule: str | None = None
            if lower_tool in {"websearch", "webfetch"}:
                rule = "external-web-tool"
            elif lower_tool == "bash" and _EXTERNAL_COMMAND.search(
                _bash_command(tool_input)
            ):
                rule = "external-command"
            if rule is not None:
                violations.append(
                    {
                        "session_id": session_id,
                        "tool": tool_name,
                        "rule": rule,
                    }
                )
    return violations
