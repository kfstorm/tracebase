"""ChatGPT ordinary-conversation discovery and source-native hydration."""

from __future__ import annotations

import json
import os
import platform
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth  # type: ignore[import-untyped]

from .archive import ArchiveError, CollectionRange, CollectionRun, Snapshot
from .collector import CollectionContext, CollectionResult
from .progress import ProgressEvent, ProgressReporter

_CHATGPT_URL = "https://chatgpt.com"
_API_BASE = _CHATGPT_URL
_DISCOVERY_LIMIT = 100
_NUM_TURNS = 100
_MAX_DISCOVERY_PASSES = 3
_REQUEST_RETRIES = 2
_RETRY_DELAYS = (0.5, 1.0)
_REQUEST_TIMEOUT_MS = 60_000
_SUCCESS_STATUS = 200
_SUCCESS_STATUS_UPPER = 300
_AUTH_FAILURE_STATUS = 401
_BROWSER_VERIFICATION_STATUS = 403
_RATE_LIMIT_STATUS = 429
_SERVER_ERROR_STATUS = 500
_AUTH_WAIT_SECONDS = 600
_AUTH_POLL_SECONDS = 2
_DISCOVERY_PARTITIONS = (
    (False, False),
    (False, True),
    (True, False),
    (True, True),
)
_sleep = time.sleep
_monotonic = time.monotonic

if platform.system() == "Windows":
    import msvcrt
else:
    import fcntl


class _Page(Protocol):
    def evaluate(self, _expression: str, _arg: Any = None) -> Any: ...

    def goto(self, url: str, **_kwargs: Any) -> Any: ...


class _BrowserContext(Protocol):
    pages: list[_Page]

    def new_page(self) -> _Page: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ChatGPTContext(CollectionContext):
    """Resolved ChatGPT account context used by a Collection Run."""

    def __post_init__(self) -> None:
        if self.source_kind != "chatgpt" or not self.scope_id:
            raise ArchiveError("ChatGPT context identity is invalid")


@dataclass(frozen=True, slots=True)
class _Response:
    body: bytes
    status: int
    headers: dict[str, str]
    observed_at: str


@dataclass(frozen=True, slots=True)
class _Session:
    account_id: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    source_id: str
    update_time: datetime
    discovery_entries: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Evidence:
    path: str
    endpoint: str
    params: dict[str, str]
    response: _Response
    page_kind: str
    page_order: int
    hydration_id: str


class ChatGPTError(ArchiveError):
    """A provider failure that must prevent Collection Run publication."""


class ChatGPTAuthenticationError(ChatGPTError):
    """The persistent browser profile has no usable authenticated session."""


class ChatGPTBrowserVerificationError(ChatGPTAuthenticationError):
    """ChatGPT returned a browser-verification response."""


def profile_path() -> Path:
    """Return the Tracebase-owned profile path, outside any archive root."""

    system = platform.system()
    if system == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "tracebase" / "chatgpt-browser"


class _ProfileLock:
    """Process lock preventing two browser contexts from sharing one profile."""

    def __init__(self, profile: Path):
        self._profile = profile
        self._path = profile.with_name(profile.name + ".lock")
        self._file: Any = None

    def __enter__(self) -> _ProfileLock:
        _secure_directory(self._path.parent)
        _secure_directory(self._profile)
        if self._path.is_symlink() or (
            self._path.exists() and not self._path.is_file()
        ):
            raise ArchiveError("ChatGPT browser profile lock is invalid")
        try:
            self._file = self._path.open("a+b")
            if platform.system() != "Windows":
                self._path.chmod(0o600)
            if platform.system() == "Windows":
                self._file.seek(0)
                self._file.write(b"0")
                self._file.flush()
                self._file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    self._file.fileno(),
                    msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if self._file is not None:
                self._file.close()
            self._file = None
            raise ArchiveError("ChatGPT browser profile is already in use") from None
        except OSError as error:
            if self._file is not None:
                self._file.close()
            self._file = None
            raise ArchiveError("ChatGPT browser profile lock is unavailable") from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._file is None:
            return
        file = self._file
        self._file = None
        try:
            if platform.system() == "Windows":
                file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    file.fileno(),
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()


def _secure_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ArchiveError("ChatGPT browser profile directory is invalid")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if platform.system() != "Windows":
            path.chmod(0o700)
    except OSError as error:
        raise ArchiveError(
            "ChatGPT browser profile directory is unavailable"
        ) from error


def _stealth_page(page: _Page) -> None:
    Stealth().apply_stealth_sync(page)


class _Browser:
    def __init__(self, profile: Path, *, headless: bool, stealth: bool):
        self.profile = profile
        self.headless = headless
        self.stealth = stealth
        self._lock: _ProfileLock | None = None
        self._playwright: Any = None
        self.context: _BrowserContext | None = None
        self.page: _Page | None = None

    def __enter__(self) -> _Browser:
        self._lock = _ProfileLock(self.profile)
        self._lock.__enter__()
        try:
            self._playwright = sync_playwright().start()
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile),
                headless=self.headless,
            )
            self.page = (
                self.context.pages[0] if self.context.pages else self.context.new_page()
            )
            if self.stealth:
                _stealth_page(self.page)
            self.page.goto(
                _CHATGPT_URL,
                wait_until="domcontentloaded",
                timeout=_REQUEST_TIMEOUT_MS,
            )
            return self
        except ChatGPTError:
            self._close()
            raise
        except Exception as error:
            self._close()
            raise ChatGPTError("ChatGPT browser could not be started") from error

    def __exit__(self, *_: object) -> None:
        self._close()

    def _close(self) -> None:
        if self.context is not None:
            with suppress(Exception):
                self.context.close()
            self.context = None
        if self._playwright is not None:
            with suppress(Exception):
                self._playwright.stop()
            self._playwright = None
        if self._lock is not None:
            self._lock.__exit__(None, None, None)
            self._lock = None


def _observed_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ChatGPTError(f"ChatGPT {label} timestamp is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ChatGPTError(f"ChatGPT {label} timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChatGPTError(f"ChatGPT {label} timestamp is invalid")
    return parsed


def _page_request_script() -> str:
    return """
async ({url, token, accountId}) => {
  const headers = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
    headers["ChatGPT-Account-Id"] = accountId;
  }
  const response = await fetch(url, {headers});
  return {
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    body: await response.text(),
  };
}
"""


class _ChatGPTAPI:
    """Small provider-only transport; credentials never leave this object."""

    def __init__(self, page: _Page):
        self.page = page
        self._access_token: str | None = None
        self._account_id: str | None = None

    @property
    def account_id(self) -> str:
        if self._account_id is None:
            raise ChatGPTAuthenticationError("ChatGPT authentication is required")
        return self._account_id

    def session(self) -> _Session:
        response = self.request("/api/auth/session", auth=False)
        if response.status in (_AUTH_FAILURE_STATUS, _BROWSER_VERIFICATION_STATUS):
            self._raise_status(response.status)
        try:
            value = json.loads(response.body)
        except json.JSONDecodeError:
            raise ChatGPTAuthenticationError(
                "ChatGPT session response is invalid"
            ) from None
        account = value.get("account") if isinstance(value, dict) else None
        token = value.get("accessToken") if isinstance(value, dict) else None
        account_id = account.get("id") if isinstance(account, dict) else None
        if (
            response.status != _SUCCESS_STATUS
            or not isinstance(token, str)
            or not token
            or not isinstance(account_id, str)
            or not account_id
        ):
            raise ChatGPTAuthenticationError("ChatGPT authentication is required")
        self._access_token = token
        self._account_id = account_id
        return _Session(account_id)

    def request(
        self,
        endpoint: str,
        params: dict[str, str] | None = None,
        *,
        auth: bool = True,
    ) -> _Response:
        if auth and (self._access_token is None or self._account_id is None):
            raise ChatGPTAuthenticationError("ChatGPT authentication is required")
        query = urlencode(params or {})
        url = f"{_API_BASE}{endpoint}"
        if query:
            url += "?" + query
        last_status = 0
        for attempt in range(_REQUEST_RETRIES + 1):
            if attempt:
                _sleep(_RETRY_DELAYS[attempt - 1])
            try:
                result = self.page.evaluate(
                    _page_request_script(),
                    {
                        "url": url,
                        "token": self._access_token if auth else None,
                        "accountId": self._account_id if auth else None,
                    },
                )
            except Exception as error:
                if attempt < _REQUEST_RETRIES:
                    continue
                raise ChatGPTError(
                    f"ChatGPT request failed: endpoint={endpoint}"
                ) from error
            if not isinstance(result, dict):
                raise ChatGPTError("ChatGPT response was invalid")
            status = result.get("status")
            body = result.get("body")
            headers = result.get("headers")
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not isinstance(body, str)
                or not isinstance(headers, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                )
            ):
                raise ChatGPTError("ChatGPT response was invalid")
            response = _Response(body.encode(), status, headers, _observed_at())
            last_status = status
            if status == _AUTH_FAILURE_STATUS:
                self._raise_status(status)
            if status == _BROWSER_VERIFICATION_STATUS:
                self._raise_status(status)
            if status == _RATE_LIMIT_STATUS or status >= _SERVER_ERROR_STATUS:
                if attempt < _REQUEST_RETRIES:
                    continue
                raise ChatGPTError(
                    f"ChatGPT request failed after retries: endpoint={endpoint}, "
                    f"status={status}, attempts={attempt + 1}"
                )
            if not _SUCCESS_STATUS <= status < _SUCCESS_STATUS_UPPER:
                raise ChatGPTError(
                    f"ChatGPT request failed: endpoint={endpoint}, status={status}"
                )
            return response
        raise ChatGPTError(
            f"ChatGPT request failed after retries: endpoint={endpoint}, "
            f"status={last_status}, attempts={_REQUEST_RETRIES + 1}"
        )

    @staticmethod
    def _raise_status(status: int) -> None:
        if status == _AUTH_FAILURE_STATUS:
            raise ChatGPTAuthenticationError("ChatGPT authentication expired")
        if status == _BROWSER_VERIFICATION_STATUS:
            raise ChatGPTBrowserVerificationError(
                "ChatGPT browser verification is required"
            )
        raise ChatGPTError(f"ChatGPT request failed: status={status}")

    @staticmethod
    def json(response: _Response) -> dict[str, Any] | list[Any]:
        try:
            value = json.loads(response.body)
        except json.JSONDecodeError:
            raise ChatGPTError("ChatGPT response was invalid JSON") from None
        if not isinstance(value, (dict, list)):
            raise ChatGPTError("ChatGPT response was invalid")
        return value


def _query_endpoint(endpoint: str, params: dict[str, str]) -> str:
    return f"{endpoint}?{urlencode(params)}"


def _discovery_page(
    value: dict[str, Any] | list[Any],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ChatGPTError("ChatGPT discovery response was invalid")
    items = value["items"]
    if not all(isinstance(item, dict) for item in items):
        raise ChatGPTError("ChatGPT discovery response was invalid")
    returned_limit = value.get("limit", _DISCOVERY_LIMIT)
    if (
        isinstance(returned_limit, bool)
        or not isinstance(returned_limit, int)
        or returned_limit <= 0
    ):
        raise ChatGPTError("ChatGPT discovery response was invalid")
    return items, returned_limit


def _ordinary(item: dict[str, Any]) -> bool:
    return (
        item.get("is_temporary_chat") is False
        and item.get("is_automation_conversation") is False
    )


def _unique_entries(
    entries: tuple[dict[str, Any], ...], additions: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    result = list(entries)
    for entry in additions:
        if entry not in result:
            result.append(entry)
    return tuple(result)


def _discover_partition(
    api: _ChatGPTAPI,
    collection_range: CollectionRange,
    is_archived: bool,
    is_starred: bool,
    pass_number: int,
    reporter: ProgressReporter,
) -> tuple[dict[str, _Candidate], dict[str, Any]]:
    candidates: dict[str, _Candidate] = {}
    offset = 0
    pages = 0
    listed = 0
    previous_update: datetime | None = None
    ordering_verified = True
    early_stopped = False
    while True:
        params = {
            "offset": str(offset),
            "limit": str(_DISCOVERY_LIMIT),
            "order": "updated",
            "is_archived": str(is_archived).lower(),
            "is_starred": str(is_starred).lower(),
        }
        endpoint = "/backend-api/conversations"
        response = api.request(endpoint, params)
        items, returned_limit = _discovery_page(api.json(response))
        pages += 1
        listed += len(items)
        for item in items:
            source_id = item.get("id")
            if not isinstance(source_id, str) or not source_id:
                raise ChatGPTError("ChatGPT discovery item is invalid")
            updated = _parse_timestamp(item.get("update_time"), "conversation update")
            if previous_update is not None and updated > previous_update:
                ordering_verified = False
            previous_update = updated
            if not _ordinary(item) or not (
                collection_range.start <= updated < collection_range.end
            ):
                continue
            entry = {
                "pass": pass_number,
                "partition": {
                    "is_archived": is_archived,
                    "is_starred": is_starred,
                },
                "endpoint": _query_endpoint(endpoint, params),
            }
            prior = candidates.get(source_id)
            if prior is None:
                candidates[source_id] = _Candidate(source_id, updated, (entry,))
            else:
                candidates[source_id] = _Candidate(
                    source_id,
                    prior.update_time,
                    _unique_entries(prior.discovery_entries, (entry,)),
                )
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="chatgpt.discover",
                completed=pages,
                message=(
                    f"pass {pass_number}, archived={is_archived}, "
                    f"starred={is_starred}, page {pages}"
                ),
            )
        )
        if (
            items
            and ordering_verified
            and previous_update is not None
            and previous_update < collection_range.start
        ):
            early_stopped = True
            break
        if len(items) < returned_limit:
            break
        offset += returned_limit
    return candidates, {
        "pass": pass_number,
        "partition": {"is_archived": is_archived, "is_starred": is_starred},
        "pages": pages,
        "listed_items": listed,
        "pagination_complete": True,
        "ordering_verified": ordering_verified,
        "early_stopped": early_stopped,
    }


def _discover(
    api: _ChatGPTAPI,
    collection_range: CollectionRange,
    reporter: ProgressReporter,
) -> tuple[dict[str, _Candidate], list[dict[str, Any]], int]:
    candidates: dict[str, _Candidate] = {}
    coverage: list[dict[str, Any]] = []
    for pass_number in range(1, _MAX_DISCOVERY_PASSES + 1):
        before = set(candidates)
        for is_archived, is_starred in _DISCOVERY_PARTITIONS:
            partition_candidates, partition_coverage = _discover_partition(
                api,
                collection_range,
                is_archived,
                is_starred,
                pass_number,
                reporter,
            )
            coverage.append(partition_coverage)
            for source_id, candidate in partition_candidates.items():
                prior = candidates.get(source_id)
                if prior is None:
                    candidates[source_id] = candidate
                else:
                    candidates[source_id] = _Candidate(
                        source_id,
                        prior.update_time,
                        _unique_entries(
                            prior.discovery_entries, candidate.discovery_entries
                        ),
                    )
        if pass_number > 1 and set(candidates) == before:
            return candidates, coverage, pass_number
    raise ChatGPTError("ChatGPT discovery did not stabilize; collection is incomplete")


def _page_info(value: dict[str, Any]) -> tuple[str | None, bool]:
    page_info = value.get("page_info")
    if not isinstance(page_info, dict):
        raise ChatGPTError("ChatGPT conversation pagination response was invalid")
    start_cursor = page_info.get("start_cursor")
    has_previous = page_info.get("has_previous_page")
    if start_cursor is not None and not isinstance(start_cursor, str):
        raise ChatGPTError("ChatGPT conversation pagination response was invalid")
    if not isinstance(has_previous, bool):
        raise ChatGPTError("ChatGPT conversation pagination response was invalid")
    return start_cursor, has_previous


def _hydrate(run: CollectionRun, api: _ChatGPTAPI, candidate: _Candidate) -> None:
    if run.has_staged_snapshot("conversation", candidate.source_id):
        return
    run.discard_staged_snapshot("conversation", candidate.source_id)
    hydration_id = str(uuid.uuid4())
    observed_from = _observed_at()
    encoded_id = quote(candidate.source_id, safe="")
    detail_endpoint = f"/backend-api/conversations/{encoded_id}"
    detail_params = {
        "include_has_versions": "true",
        "num_turns": str(_NUM_TURNS),
    }
    detail_response = api.request(detail_endpoint, detail_params)
    detail = api.json(detail_response)
    if not isinstance(detail, dict) or not isinstance(detail.get("messages"), list):
        raise ChatGPTError("ChatGPT conversation response was invalid")
    cursor, has_previous = _page_info(detail)
    evidence = [
        _Evidence(
            "conversation.json",
            detail_endpoint,
            detail_params,
            detail_response,
            "detail",
            1,
            hydration_id,
        )
    ]
    page_order = 2
    seen_cursors: set[str] = set()
    while has_previous:
        if not cursor or cursor in seen_cursors:
            raise ChatGPTError("ChatGPT conversation pagination did not progress")
        seen_cursors.add(cursor)
        older_endpoint = f"/backend-api/conversations/{encoded_id}/messages"
        older_params = {
            "before": cursor,
            "num_turns": str(_NUM_TURNS),
            "include_has_versions": "true",
        }
        older_response = api.request(older_endpoint, older_params)
        older = api.json(older_response)
        if not isinstance(older, dict) or not isinstance(older.get("messages"), list):
            raise ChatGPTError("ChatGPT older conversation response was invalid")
        cursor, has_previous = _page_info(older)
        evidence.append(
            _Evidence(
                f"messages.{page_order - 1:03d}.json",
                older_endpoint,
                older_params,
                older_response,
                "older-messages",
                page_order,
                hydration_id,
            )
        )
        page_order += 1
    observation_to = _observed_at()
    snapshot = Snapshot(
        source_kind="chatgpt",
        object_kind="conversation",
        source_id=candidate.source_id,
        observation_window={"from": observed_from, "to": observation_to},
        evidence_files=tuple(
            {
                "path": item.path,
                "request": {
                    "endpoint": item.endpoint,
                    "method": "GET",
                    "api": "ChatGPT Web API",
                    "query": item.params,
                    "response_status": item.response.status,
                    "response_headers": {
                        key: value
                        for key, value in item.response.headers.items()
                        if key.lower() in {"content-type", "etag", "last-modified"}
                    },
                    "observed_at": item.response.observed_at,
                    "hydration_id": item.hydration_id,
                    "page_kind": item.page_kind,
                    "page_order": item.page_order,
                },
            }
            for item in evidence
        ),
        selection_provenance=(
            {
                "update_time": candidate.update_time.isoformat(),
                "ordinary_predicate": [
                    "is_temporary_chat === false",
                    "is_automation_conversation === false",
                ],
                "discovery_entries": list(candidate.discovery_entries),
            },
        ),
        metadata={
            "representation": (
                "current branch plus provider-returned historical records"
            ),
            "branch_recovery": "not_complete",
            "message_pagination": "backward_before_start_cursor",
        },
    )
    snapshot_root = run.write_snapshot(snapshot)
    for item in evidence:
        run.write_evidence(snapshot_root, item.path, item.response.body)


def resolve_context() -> ChatGPTContext:
    """Resolve and validate the authenticated account for a Collection Run."""

    try:
        return _resolve_context_with_browser(headless=True)
    except ChatGPTBrowserVerificationError:
        if not _display_fallback_available():
            raise
        return _resolve_context_with_browser(headless=False)


def _resolve_context_with_browser(*, headless: bool) -> ChatGPTContext:
    with _Browser(profile_path(), headless=headless, stealth=True) as browser:
        if browser.page is None:
            raise ChatGPTError("ChatGPT browser page is unavailable")
        session = _ChatGPTAPI(browser.page).session()
    return ChatGPTContext(
        source_kind="chatgpt",
        scope_id=session.account_id,
        collector_version="0.1.0",
        effective_options={
            "browser_execution": (
                "stealth-headless" if headless else "stealth-headed-display-fallback"
            ),
            "discovery_limit": _DISCOVERY_LIMIT,
            "message_page_size": _NUM_TURNS,
        },
    )


def _authenticated_api(browser: _Browser) -> _ChatGPTAPI:
    if browser.page is None:
        raise ChatGPTError("ChatGPT browser page is unavailable")
    api = _ChatGPTAPI(browser.page)
    api.session()
    return api


def _collect_api(
    run: CollectionRun, reporter: ProgressReporter, api: _ChatGPTAPI
) -> CollectionResult:
    if api.account_id != run.scope_id:
        raise ChatGPTAuthenticationError("ChatGPT account changed during collection")
    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="chatgpt.discover",
            label="Discovering ChatGPT conversations",
        )
    )
    candidates, discovery, passes = _discover(api, run.collection_range, reporter)
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="chatgpt.discover",
            message=f"{len(candidates)} conversations",
        )
    )
    reporter.emit(
        ProgressEvent(
            kind="start",
            task_id="chatgpt.hydrate",
            label="Hydrating ChatGPT conversations",
            total=len(candidates),
        )
    )
    for completed, candidate in enumerate(candidates.values(), start=1):
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="chatgpt.hydrate",
                completed=completed - 1,
                total=len(candidates),
                phase="starting",
                current=candidate.source_id,
            )
        )
        _hydrate(run, api, candidate)
        reporter.emit(
            ProgressEvent(
                kind="update",
                task_id="chatgpt.hydrate",
                completed=completed,
                total=len(candidates),
                phase="completed",
                current=candidate.source_id,
            )
        )
    reporter.emit(
        ProgressEvent(
            kind="finish",
            task_id="chatgpt.hydrate",
            completed=len(candidates),
            message=f"{len(candidates)} conversations",
        )
    )
    return CollectionResult(
        coverage={
            "discovery_endpoint": "/backend-api/conversations",
            "discovery_partitions": [
                {"is_archived": archived, "is_starred": starred}
                for archived, starred in _DISCOVERY_PARTITIONS
            ],
            "discovery_limit": _DISCOVERY_LIMIT,
            "discovery_passes": passes,
            "discovery_pages": discovery,
            "pagination_complete": True,
            "selected_conversations": len(candidates),
            "offset_pagination_limitation": (
                "Provider has no verified snapshot/cursor guarantee; repeated full "
                "passes reduce mutable-update skip risk but cannot prove a stable "
                "snapshot."
            ),
            "source_limitations": [
                "Deleted conversations unavailable to retrospective discovery.",
                "Hydration is current branch plus provider-returned historical "
                "records, "
                "not complete branch recovery.",
                "Canvas/textdocs are outside this collector's v1 representation.",
            ],
        }
    )


def collect(run: CollectionRun, reporter: ProgressReporter) -> CollectionResult:
    """Discover and fully hydrate ordinary conversations in the range."""

    try:
        return _collect_with_browser(run, reporter, headless=True)
    except ChatGPTBrowserVerificationError:
        if not _display_fallback_available():
            raise
        return _collect_with_browser(run, reporter, headless=False)


def _collect_with_browser(
    run: CollectionRun, reporter: ProgressReporter, *, headless: bool
) -> CollectionResult:
    with _Browser(profile_path(), headless=headless, stealth=True) as browser:
        api = _authenticated_api(browser)
        return _collect_api(run, reporter, api)


def _display_fallback_available() -> bool:
    return platform.system() == "Linux" and bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def _session_is_valid(api: _ChatGPTAPI) -> bool:
    try:
        api.session()
    except ChatGPTAuthenticationError:
        return False
    return True


def authenticate() -> None:
    """Open the headed profile and wait for the user to complete authentication."""

    with _Browser(profile_path(), headless=False, stealth=False) as browser:
        if browser.page is None:
            raise ChatGPTError("ChatGPT browser page is unavailable")
        api = _ChatGPTAPI(browser.page)
        deadline = _monotonic() + _AUTH_WAIT_SECONDS
        while _monotonic() < deadline:
            if _session_is_valid(api):
                return
            _sleep(_AUTH_POLL_SECONDS)
    raise ChatGPTAuthenticationError(
        "ChatGPT authentication was not completed before the timeout"
    )


__all__ = [
    "ChatGPTAuthenticationError",
    "ChatGPTBrowserVerificationError",
    "ChatGPTContext",
    "ChatGPTError",
    "_Candidate",
    "_ChatGPTAPI",
    "_Response",
    "_collect_api",
    "_discover",
    "_hydrate",
    "authenticate",
    "collect",
    "profile_path",
    "resolve_context",
]
