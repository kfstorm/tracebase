"""Same-Major credential initialization through supported OpenCode interfaces."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .opencode_version import V1_MAJOR, V2_MAJOR, supported_major


class OpenCodeError(RuntimeError):
    """Raised when isolated OpenCode state cannot be prepared."""


@dataclass(frozen=True)
class HostOpenCode:
    executable: str
    version: str
    major: int


def detect_host() -> HostOpenCode:
    executable = shutil.which("opencode")
    if executable is None:
        raise OpenCodeError("Could not detect host OpenCode CLI")
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, check=False
        )
    except OSError:
        raise OpenCodeError("Could not detect host OpenCode version") from None
    if result.returncode != 0:
        raise OpenCodeError("Could not detect host OpenCode version")
    try:
        version = result.stdout.decode("utf-8").strip().removeprefix("opencode v")
        major = supported_major(version)
    except UnicodeError, ValueError:
        raise OpenCodeError("Host OpenCode version is invalid or unsupported") from None
    return HostOpenCode(executable, version, major)


def validate_summary_versions(container_version: str) -> HostOpenCode:
    host = detect_host()
    try:
        major = supported_major(container_version)
    except ValueError:
        raise OpenCodeError(
            "Summary container OpenCode version is invalid or unsupported"
        ) from None
    if host.major != major:
        raise OpenCodeError(
            "Summary requires matching OpenCode major versions: "
            f"host={host.version}, container={container_version}. "
            "Cross-major credential transfer is not supported."
        )
    return host


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


def prepare_config(stage_model: str) -> str:
    """Build the OpenCode configuration without reading host state."""
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": stage_model,
        "small_model": stage_model,
        "subagent_depth": 1,
    }
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def prepare_state(
    state_dir: Path, stage_model: str, host: HostOpenCode, image: str
) -> str:
    """Initialize one Summary's private state after its version check."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    opencode_data = state_dir / "share/opencode"
    opencode_data.mkdir(parents=True, exist_ok=True, mode=0o700)
    opencode_data.chmod(0o700)
    if host.major == V2_MAJOR:
        _import_v2_state(state_dir, host, image)
        return prepare_config(stage_model)
    if host.major != V1_MAJOR:
        raise OpenCodeError("Unsupported OpenCode major version")
    auth_path = _auth_path()
    auth = _read_json(auth_path)
    isolated_auth = opencode_data / "auth.json"
    isolated_auth.write_text(
        json.dumps(auth, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    isolated_auth.chmod(0o600)

    return prepare_config(stage_model)


def _import_v2_state(state_dir: Path, host: HostOpenCode, image: str) -> None:
    try:
        # --standalone forces the actual host CLI's local credential environment,
        # rather than a configured remote or background server connection.
        exported = subprocess.run(
            [host.executable, "--standalone", "auth", "export"],
            capture_output=True,
            check=False,
        )
    except OSError:
        raise OpenCodeError("OpenCode V2 credential export failed") from None
    if exported.returncode != 0:
        raise OpenCodeError("OpenCode V2 credential export failed")
    try:
        imported = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--network",
                "none",
                "--read-only",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--tmpfs",
                "/tmp:rw,nosuid,size=256m",
                "--tmpfs",
                f"/home/eval:rw,nosuid,uid={os.getuid()},gid={os.getgid()},mode=700,size=256m",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--env",
                "HOME=/home/eval",
                "--env",
                "XDG_DATA_HOME=/home/eval/.local/share",
                "--env",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS=true",
                "--env",
                "OPENCODE_DISABLE_MODELS_FETCH=true",
                "--volume",
                f"{state_dir}:/home/eval/.local:rw",
                "--entrypoint",
                "opencode",
                image,
                "--standalone",
                "auth",
                "import",
            ],
            input=exported.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        raise OpenCodeError("OpenCode V2 credential import failed") from None
    if imported.returncode != 0:
        raise OpenCodeError("OpenCode V2 credential import failed")
