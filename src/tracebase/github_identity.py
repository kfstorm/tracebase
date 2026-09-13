"""Persistent local identity data used for GitHub attribution."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive import ArchiveError, encode_path_id

_PROFILE_FORMAT_VERSION = 2
_MODERN_NOREPLY = re.compile(
    r"^(?P<numeric_id>[0-9]+)\+[^@]+@users\.noreply\.github\.com$"
)
_LEGACY_NOREPLY = re.compile(r"^(?P<login>[^@]+)@users\.noreply\.github\.com$")


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    """Parsed archive-bound GitHub identity data for one stable account."""

    scope_id: str
    login: str
    numeric_id: int
    emails: frozenset[str]
    historical_logins: frozenset[str]
    synced_at: str

    def matches_commit_email(self, value: Any) -> bool:
        normalized = normalize_email(value)
        if normalized is None:
            return False
        if normalized in self.emails:
            return True
        modern = _MODERN_NOREPLY.fullmatch(normalized)
        if modern is not None:
            return int(modern.group("numeric_id")) == self.numeric_id
        legacy = _LEGACY_NOREPLY.fullmatch(normalized)
        return legacy is not None and legacy.group("login") in self.historical_logins


def identity_profile_path(archive_root: str | Path, scope_id: str) -> Path:
    if not isinstance(scope_id, str) or not scope_id:
        raise ArchiveError("GitHub source scope ID is invalid for an identity profile")
    return (
        Path(archive_root).absolute() / "profiles" / "github" / encode_path_id(scope_id)
    )


def normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized or None


def _json_bytes(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _profile_error(scope_id: str, path: Path, reason: str) -> ArchiveError:
    return ArchiveError(
        f"GitHub identity profile for source scope {scope_id} {reason}:\n"
        f"  {path}\n\nRun:\n  {_sync_command(path.parents[2])}"
    )


def _sync_command(archive_root: str | Path) -> str:
    return "tracebase identity github sync --archive " + shlex.quote(
        str(Path(archive_root))
    )


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise OSError
    return path.read_bytes()


def _parse_identity(  # noqa: PLR0911
    profile_root: Path, scope_id: str, historical_logins: set[str]
) -> GitHubIdentity | None:
    profile_path = profile_root / "profile.json"
    try:
        metadata = _json_bytes(_read_regular(profile_path))
    except OSError, ValueError:
        return None
    if not isinstance(metadata, dict):
        return None
    response_files = metadata.get("response_files")
    synced_at = metadata.get("synced_at")
    if (
        metadata.get("provider") != "github"
        or metadata.get("format_version") != _PROFILE_FORMAT_VERSION
        or metadata.get("scope_id") != scope_id
        or not isinstance(synced_at, str)
        or not isinstance(response_files, list)
        or not all(isinstance(path, str) for path in response_files)
        or not response_files
        or response_files[0] != "user.json"
        or response_files[1:]
        != [f"emails.{index:03d}.json" for index in range(1, len(response_files))]
    ):
        return None
    try:
        user = _json_bytes(_read_regular(profile_root / "user.json"))
    except OSError, ValueError:
        return None
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    numeric_id = user.get("id")
    if (
        user.get("node_id") != scope_id
        or not isinstance(login, str)
        or not login
        or not isinstance(numeric_id, int)
        or isinstance(numeric_id, bool)
    ):
        return None
    emails: set[str] = set()
    for path in response_files[1:]:
        try:
            page = _json_bytes(_read_regular(profile_root / path))
        except OSError, ValueError:
            return None
        if not isinstance(page, list):
            return None
        for entry in page:
            if not isinstance(entry, dict):
                return None
            if (email := normalize_email(entry.get("email"))) is not None:
                emails.add(email)
    return GitHubIdentity(
        scope_id,
        login,
        numeric_id,
        frozenset(emails),
        frozenset(login.casefold() for login in historical_logins),
        synced_at,
    )


def require_github_identity(
    archive_root: str | Path,
    scope_id: str,
    historical_logins: set[str] | None = None,
) -> GitHubIdentity:
    """Load one archive-bound profile or raise an actionable archive error."""
    try:
        profile_root = identity_profile_path(archive_root, scope_id)
    except ArchiveError:
        raise ArchiveError(
            "GitHub identity profile for source scope "
            f"{scope_id} has an invalid ID.\n\n"
            f"Run:\n  {_sync_command(archive_root)}"
        ) from None
    try:
        identity = _parse_identity(profile_root, scope_id, historical_logins or set())
    except OSError, ValueError:
        identity = None
    if identity is None:
        if not profile_root.exists():
            reason = "was not found in this archive"
        else:
            reason = "is invalid"
        raise _profile_error(scope_id, profile_root, reason)
    return identity


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _replace_profile(profile_root: Path, temporary_root: Path) -> None:
    """Install a staged profile and best-effort restore on install failure."""
    parent = profile_root.parent
    previous_root: Path | None = None
    if profile_root.exists() or profile_root.is_symlink():
        if profile_root.is_symlink() or not profile_root.is_dir():
            raise ArchiveError("GitHub identity profile path is not a directory")
        previous_root = parent / f".{profile_root.name}.previous-{uuid.uuid4().hex}"
        profile_root.replace(previous_root)
    try:
        temporary_root.replace(profile_root)
    except OSError:
        if previous_root is not None and not profile_root.exists():
            with suppress(OSError):
                previous_root.replace(profile_root)
        raise
    if previous_root is not None:
        with suppress(OSError):
            shutil.rmtree(previous_root)


def save_github_identity(
    archive_root: str | Path, user_body: bytes, email_bodies: tuple[bytes, ...]
) -> Path:
    """Stage complete GitHub identity response bodies before replacement."""
    user = _json_bytes(user_body)
    if not isinstance(user, dict):
        raise ArchiveError("GitHub identity response was invalid")
    scope_id = user.get("node_id")
    login = user.get("login")
    numeric_id = user.get("id")
    if (
        not isinstance(scope_id, str)
        or not scope_id
        or not isinstance(login, str)
        or not login
        or not isinstance(numeric_id, int)
        or isinstance(numeric_id, bool)
    ):
        raise ArchiveError("GitHub identity response was invalid")
    for body in email_bodies:
        page = _json_bytes(body)
        if not isinstance(page, list) or not all(
            isinstance(entry, dict) for entry in page
        ):
            raise ArchiveError("GitHub identity email response was invalid")
    profile_root = identity_profile_path(archive_root, scope_id)
    parent = profile_root.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
        parent.parent.chmod(0o700)
        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{profile_root.name}.", dir=parent)
        )
        temporary_root.chmod(0o700)
        response_files = ["user.json"] + [
            f"emails.{index:03d}.json" for index in range(1, len(email_bodies) + 1)
        ]
        metadata = {
            "format_version": _PROFILE_FORMAT_VERSION,
            "provider": "github",
            "response_files": response_files,
            "scope_id": scope_id,
            "synced_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        _write_private(
            temporary_root / "profile.json",
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(),
        )
        _write_private(temporary_root / "user.json", user_body)
        for index, body in enumerate(email_bodies, start=1):
            _write_private(temporary_root / f"emails.{index:03d}.json", body)
        _replace_profile(profile_root, temporary_root)
    except ArchiveError:
        if "temporary_root" in locals() and temporary_root.exists():
            shutil.rmtree(temporary_root)
        raise
    except (OSError, ValueError) as error:
        if "temporary_root" in locals() and temporary_root.exists():
            shutil.rmtree(temporary_root)
        raise ArchiveError("GitHub identity profile could not be written") from error
    return profile_root
