"""Persistent local identity data used for GitHub attribution."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive import ArchiveError


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    """Non-archived GitHub identity data for the local tracked account."""

    login: str
    numeric_id: int
    emails: frozenset[str]
    noreply_aliases: frozenset[str]
    synced_at: str

    @property
    def commit_identities(self) -> frozenset[str]:
        return self.emails | self.noreply_aliases


def identity_profile_path(archive_root: str | Path, login: str) -> Path:
    if (
        not isinstance(login, str)
        or not login
        or login in {".", ".."}
        or "/" in login
        or "\\" in login
    ):
        raise ArchiveError("GitHub login is invalid for an identity profile")
    return Path(archive_root).absolute() / "profiles" / "github" / f"{login}.json"


def normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized or None


def load_github_identity(
    archive_root: str | Path, login: str | None
) -> GitHubIdentity | None:
    if login is None:
        return None
    requested_login = login
    try:
        path = identity_profile_path(archive_root, login)
    except ArchiveError:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    login = value.get("login")
    numeric_id = value.get("numeric_id")
    synced_at = value.get("synced_at")
    emails = value.get("emails")
    aliases = value.get("noreply_aliases")
    if (
        not isinstance(login, str)
        or not login
        or login != requested_login
        or not isinstance(numeric_id, int)
        or isinstance(numeric_id, bool)
        or not isinstance(synced_at, str)
        or not isinstance(emails, list)
        or not isinstance(aliases, list)
    ):
        return None
    normalized_emails = frozenset(
        normalized
        for email in emails
        if (normalized := normalize_email(email)) is not None
    )
    normalized_aliases = frozenset(
        normalized
        for alias in aliases
        if (normalized := normalize_email(alias)) is not None
    )
    return GitHubIdentity(
        login,
        numeric_id,
        normalized_emails,
        normalized_aliases,
        synced_at,
    )


def _write_profile(archive_root: str | Path, login: str, value: dict[str, Any]) -> Path:
    path = identity_profile_path(archive_root, login)
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as error:
        raise ArchiveError("GitHub identity profile could not be written") from error
    return path


def save_github_identity(
    archive_root: str | Path,
    login: str,
    numeric_id: int,
    emails: list[str],
) -> Path:
    aliases = [
        f"{numeric_id}+{login}@users.noreply.github.com",
        f"{login}@users.noreply.github.com",
    ]
    return _write_profile(
        archive_root,
        login,
        {
            "emails": emails,
            "login": login,
            "noreply_aliases": aliases,
            "numeric_id": numeric_id,
            "provider": "github",
            "synced_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    )
