"""OpenCode state preparation and public CLI/database integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class OpenCodeError(RuntimeError):
    """Raised when isolated OpenCode state cannot be prepared."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OpenCodeError(f"could not read OpenCode JSON state: {path}") from error
    if not isinstance(value, dict):
        raise OpenCodeError(f"OpenCode JSON state is not an object: {path}")
    return value


def _auth_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    return Path(data_home or (Path.home() / ".local/share")) / "opencode/auth.json"


def prepare_state(state_dir: Path, stage_model: str) -> str:
    """Copy host authentication into a fresh run-local OpenCode state."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    opencode_data = state_dir / "share/opencode"
    opencode_data.mkdir(parents=True, exist_ok=True, mode=0o700)
    opencode_data.chmod(0o700)
    auth_path = _auth_path()
    auth = _read_json(auth_path)
    isolated_auth = opencode_data / "auth.json"
    isolated_auth.write_text(
        json.dumps(auth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    isolated_auth.chmod(0o600)

    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": stage_model,
        "small_model": stage_model,
        "subagent_depth": 1,
    }
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))
