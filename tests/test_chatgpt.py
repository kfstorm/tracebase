import json
import stat
import subprocess
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from tracebase.archive import Archive, ArchiveError, CollectionRange, CollectionRun
from tracebase.chatgpt import (
    ChatGPTAuthenticationError,
    ChatGPTBrowserVerificationError,
    ChatGPTContext,
    ChatGPTError,
    _Browser,
    _Candidate,
    _ChatGPTAPI,
    _ChatGPTSessionProbe,
    _collect_api,
    _discover,
    _discover_partition,
    _hydrate,
    _ProfileLock,
    _Response,
    _Session,
    authenticate,
    collect,
    profile_path,
    reset,
    resolve_context,
    status,
)
from tracebase.cli import _parser, main
from tracebase.collector import CollectionResult
from tracebase.progress import LineProgressSink, ProgressEvent, ProgressReporter

RANGE = CollectionRange.parse("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")


def response(value: object, status: int = 200) -> _Response:
    return _Response(
        json.dumps(value).encode(),
        status,
        {"content-type": "application/json"},
        "2026-09-13T00:00:00Z",
    )


def reporter() -> ProgressReporter:
    return ProgressReporter(LineProgressSink(StringIO()))


def discovery_reporter() -> ProgressReporter:
    progress = reporter()
    progress.emit(ProgressEvent(kind="start", task_id="chatgpt.projects"))
    progress.emit(ProgressEvent(kind="start", task_id="chatgpt.discover"))
    return progress


def item(
    source_id: str,
    update_time: str,
    *,
    archived: bool,
    starred: bool,
    automation: bool = False,
    temporary: bool = False,
    gizmo_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": source_id,
        "update_time": update_time,
        "is_archived": archived,
        "is_starred": starred,
        "is_automation_conversation": automation,
        "is_temporary_chat": temporary,
        "gizmo_id": gizmo_id,
    }


def project_item(project_id: str, name: str) -> dict[str, object]:
    return {
        "gizmo": {
            "gizmo": {
                "id": project_id,
                "display": {"name": name},
            }
        },
        "conversations": {"items": []},
    }


class DiscoveryAPI:
    account_id = "account-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.project_sidebar_pages: list[list[dict[str, object]]] = [[]]
        self.project_conversation_pages: dict[str, list[list[dict[str, object]]]] = {}
        self.pages = {
            (False, False): [
                item(
                    "active-conversation",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=False,
                ),
                item(
                    "at-start",
                    "2026-01-01T00:00:00Z",
                    archived=False,
                    starred=False,
                ),
            ],
            (False, True): [
                item(
                    "active-conversation",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=True,
                ),
                item(
                    "automation",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=True,
                    automation=True,
                ),
                item(
                    "temporary",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=True,
                    temporary=True,
                ),
                item(
                    "project-conversation",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=True,
                    gizmo_id="g-p-synthetic-project",
                ),
            ],
            (True, False): [
                item(
                    "archived-conversation",
                    "2026-01-01T00:30:00Z",
                    archived=True,
                    starred=False,
                ),
                item(
                    "custom-gpt-conversation",
                    "2026-01-01T00:30:00Z",
                    archived=True,
                    starred=False,
                    gizmo_id="g-synthetic-custom-gpt",
                ),
                item(
                    "at-end",
                    "2026-01-01T01:00:00Z",
                    archived=True,
                    starred=False,
                ),
            ],
            (True, True): [],
        }

    def request(self, endpoint: str, params: dict[str, str]) -> _Response:
        self.calls.append((endpoint, params))
        if endpoint == "/backend-api/gizmos/snorlax/sidebar":
            return response(
                self._cursor_page(self.project_sidebar_pages, params.get("cursor"))
            )
        if endpoint.startswith("/backend-api/gizmos/"):
            project_id = endpoint.split("/")[3]
            return response(
                self._cursor_page(
                    self.project_conversation_pages.get(project_id, [[]]),
                    params.get("cursor"),
                )
            )
        key = (params["is_archived"] == "true", params["is_starred"] == "true")
        offset = int(params["offset"])
        page = self.pages[key] if offset == 0 else []
        return response({"items": page, "limit": 2, "total": 999_999})

    @staticmethod
    def _cursor_page(
        pages: list[list[dict[str, object]]], cursor: str | None
    ) -> dict[str, object]:
        index = int(cursor) if cursor is not None else 0
        items = pages[index] if index < len(pages) else []
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return {"items": items, "cursor": next_cursor}

    @staticmethod
    def _begin_ordinary_pass(endpoint: str, params: dict[str, str]) -> bool:
        """Return whether a request starts a new ordinary discovery pass."""

        return (
            endpoint == "/backend-api/conversations"
            and params["offset"] == "0"
            and params["is_archived"] == "false"
            and params["is_starred"] == "false"
        )

    def _reset_pages(self) -> None:
        self.pages = {
            (False, False): [],
            (False, True): [],
            (True, False): [],
            (True, True): [],
        }

    @staticmethod
    def json(value: _Response) -> dict[str, object] | list[object]:
        return json.loads(value.body)


def test_discovery_covers_four_partitions_and_stabilizes() -> None:
    api = DiscoveryAPI()
    progress = reporter()
    progress.emit(ProgressEvent(kind="start", task_id="chatgpt.projects"))
    progress.emit(ProgressEvent(kind="start", task_id="chatgpt.discover"))
    candidates, coverage, passes, project_coverage = _discover(api, RANGE, progress)

    assert set(candidates) == {
        "active-conversation",
        "at-start",
        "archived-conversation",
    }
    assert not {
        "automation",
        "temporary",
        "project-conversation",
        "custom-gpt-conversation",
    } & set(candidates)
    assert passes == 2
    assert len(coverage) == 8
    assert {
        (call[1]["is_archived"], call[1]["is_starred"])
        for call in api.calls
        if call[0] == "/backend-api/conversations" and call[1]["offset"] == "0"
    } == {("false", "false"), ("false", "true"), ("true", "false"), ("true", "true")}
    assert all(
        call[1]["offset"] in {"0", "2"}
        for call in api.calls
        if call[0] == "/backend-api/conversations"
    )
    assert project_coverage["listed_projects"] == 0
    assert project_coverage["projects"] == []


class PartitionAPI(DiscoveryAPI):
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages
        self.calls: list[int] = []

    def request(self, endpoint: str, params: dict[str, str]) -> _Response:
        if endpoint == "/backend-api/gizmos/snorlax/sidebar":
            return response({"items": [], "cursor": None})
        assert endpoint == "/backend-api/conversations"
        offset = int(params["offset"])
        self.calls.append(offset)
        page = self.pages[offset // 2] if offset // 2 < len(self.pages) else []
        return response({"items": page, "limit": 2, "total": 999_999})


def test_discovery_stops_when_first_page_is_older_than_range() -> None:
    api = PartitionAPI(
        [
            [
                item(
                    "old-1",
                    "2025-12-31T23:00:00Z",
                    archived=False,
                    starred=False,
                ),
                item(
                    "old-2",
                    "2025-12-31T22:00:00Z",
                    archived=False,
                    starred=False,
                ),
            ]
        ]
    )

    candidates, coverage = _discover_partition(
        api, RANGE, False, False, 1, discovery_reporter()
    )

    assert candidates == {}
    assert api.calls == [0]
    assert coverage["early_stopped"] is True


def test_discovery_stops_after_range_page_enters_old_region() -> None:
    api = PartitionAPI(
        [
            [
                item(
                    "in-range",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=False,
                ),
                item(
                    "at-start",
                    "2026-01-01T00:00:00Z",
                    archived=False,
                    starred=False,
                ),
            ],
            [
                item(
                    "old",
                    "2025-12-31T23:59:59Z",
                    archived=False,
                    starred=False,
                )
            ],
        ]
    )

    candidates, coverage = _discover_partition(
        api, RANGE, False, False, 1, discovery_reporter()
    )

    assert set(candidates) == {"in-range", "at-start"}
    assert api.calls == [0, 2]
    assert coverage["early_stopped"] is True


def test_discovery_keeps_scanning_when_ordering_is_invalid() -> None:
    api = PartitionAPI(
        [
            [
                item(
                    "in-range",
                    "2026-01-01T00:30:00Z",
                    archived=False,
                    starred=False,
                ),
                item(
                    "in-range-2",
                    "2026-01-01T00:20:00Z",
                    archived=False,
                    starred=False,
                ),
            ],
            [
                item(
                    "later-in-range",
                    "2026-01-01T00:40:00Z",
                    archived=False,
                    starred=False,
                ),
                item(
                    "old-2",
                    "2025-12-31T22:00:00Z",
                    archived=False,
                    starred=False,
                ),
            ],
            [
                item(
                    "old-3",
                    "2025-12-31T21:00:00Z",
                    archived=False,
                    starred=False,
                )
            ],
        ]
    )

    candidates, coverage = _discover_partition(
        api, RANGE, False, False, 1, discovery_reporter()
    )

    assert set(candidates) == {"in-range", "in-range-2", "later-in-range"}
    assert api.calls == [0, 2, 4]
    assert coverage["early_stopped"] is False
    assert coverage["ordering_verified"] is False


def test_stabilization_finds_new_in_range_id_on_later_pass() -> None:
    class AppearingAPI(DiscoveryAPI):
        def __init__(self) -> None:
            super().__init__()
            self.pass_calls = 0

        def request(self, endpoint: str, params: dict[str, str]) -> _Response:
            if (
                endpoint == "/backend-api/conversations"
                and params["is_archived"] == "false"
                and params["is_starred"] == "false"
            ):
                self.pass_calls += 1
                if self.pass_calls == 2:
                    self.pages[(False, False)].append(
                        item(
                            "appeared-later",
                            "2026-01-01T00:45:00Z",
                            archived=False,
                            starred=False,
                        )
                    )
            return super().request(endpoint, params)

    api = AppearingAPI()

    candidates, _coverage, passes, _project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    assert "appeared-later" in candidates
    assert passes == 3


class ProjectDiscoveryAPI(DiscoveryAPI):
    def __init__(self) -> None:
        super().__init__()
        self.project_sidebar_pages = [
            [
                project_item("g-p-project-one", "Synthetic project"),
                project_item("g-p-project-two", "Second project"),
            ]
        ]
        self.project_conversation_pages = {
            "g-p-project-one": [
                [
                    item(
                        "project-only",
                        "2026-01-01T00:20:00Z",
                        archived=False,
                        starred=False,
                        gizmo_id="g-p-project-one",
                    ),
                    item(
                        "active-conversation",
                        "2026-01-01T00:30:00Z",
                        archived=False,
                        starred=False,
                        gizmo_id="g-p-project-one",
                    ),
                ]
            ],
            "g-p-project-two": [
                [
                    item(
                        "second-project-conversation",
                        "2026-01-01T00:10:00Z",
                        archived=False,
                        starred=False,
                        gizmo_id="g-p-project-two",
                    ),
                    item(
                        "project-at-start",
                        "2026-01-01T00:00:00Z",
                        archived=False,
                        starred=False,
                        gizmo_id="g-p-project-two",
                    ),
                    item(
                        "project-at-end",
                        "2026-01-01T01:00:00Z",
                        archived=False,
                        starred=False,
                        gizmo_id="g-p-project-two",
                    ),
                ]
            ],
        }
        for entry in self.pages[(False, True)]:
            if entry["id"] == "project-conversation":
                entry["gizmo_id"] = "g-p-project-one"


def _is_project_endpoint(endpoint: str, kind: str) -> bool:
    if kind == "sidebar":
        return endpoint == "/backend-api/gizmos/snorlax/sidebar"
    return endpoint.startswith("/backend-api/gizmos/") and endpoint.endswith(
        "/conversations"
    )


class ProjectEndpointOverride(ProjectDiscoveryAPI):
    """Serve a fixed payload for one kind of project discovery endpoint."""

    def __init__(self, kind: str, payload: dict[str, object]) -> None:
        super().__init__()
        self.kind = kind
        self.payload = payload

    def request(self, endpoint: str, params: dict[str, str]) -> _Response:
        if _is_project_endpoint(endpoint, self.kind):
            self.calls.append((endpoint, params))
            return response(self.payload)
        return super().request(endpoint, params)


def test_project_discovery_includes_and_deduplicates_project_conversations() -> None:
    api = ProjectDiscoveryAPI()

    candidates, _coverage, _passes, project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    assert set(candidates) == {
        "active-conversation",
        "at-start",
        "archived-conversation",
        "project-only",
        "project-conversation",
        "second-project-conversation",
        "project-at-start",
    }
    assert "project-at-end" not in candidates
    first = candidates["active-conversation"].project
    assert first is not None
    assert (first.source_id, first.name) == (
        "g-p-project-one",
        "Synthetic project",
    )
    assert candidates["project-only"].project == first
    assert candidates["project-conversation"].project == first
    second = candidates["second-project-conversation"].project
    assert second is not None
    assert (second.source_id, second.name) == (
        "g-p-project-two",
        "Second project",
    )
    # The conversation was observed in ordinary history and in its project.
    entries = candidates["active-conversation"].discovery_entries
    assert {entry.get("kind") for entry in entries} == {None, "project"}
    assert len(entries) > 2
    assert project_coverage["listed_projects"] == 2
    assert project_coverage["pagination_complete"] is True
    assert [
        (project["source_id"], project["selected_conversations"])
        for project in project_coverage["projects"]
    ] == [("g-p-project-one", 2), ("g-p-project-two", 2)]


def test_project_discovery_paginates_index_and_conversations() -> None:
    api = ProjectDiscoveryAPI()
    api.project_sidebar_pages = [
        [project_item("g-p-project-one", "Synthetic project")],
        [project_item("g-p-project-two", "Second project")],
    ]
    api.project_conversation_pages["g-p-project-one"] = [
        [
            item(
                "project-page-one",
                "2026-01-01T00:20:00Z",
                archived=False,
                starred=False,
                gizmo_id="g-p-project-one",
            )
        ],
        [
            item(
                "project-page-two",
                "2026-01-01T00:10:00Z",
                archived=False,
                starred=False,
                gizmo_id="g-p-project-one",
            )
        ],
    ]

    candidates, _coverage, passes, project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    assert "project-page-one" in candidates
    assert "project-page-two" in candidates
    assert project_coverage["pages"] == 2
    assert project_coverage["projects"][0]["conversation_pages"] == 2
    sidebar_calls = [
        params
        for endpoint, params in api.calls
        if endpoint == "/backend-api/gizmos/snorlax/sidebar"
    ]
    assert [params.get("cursor") for params in sidebar_calls] == [None, "1"]
    project_calls = [
        params
        for endpoint, params in api.calls
        if endpoint == "/backend-api/gizmos/g-p-project-one/conversations"
    ]
    assert [params.get("cursor") for params in project_calls] == [None, "1"] * passes


def test_project_discovery_rejects_stalled_pagination() -> None:
    sidebar = ProjectEndpointOverride(
        "sidebar",
        {
            "items": [project_item("g-p-project-one", "Synthetic project")],
            "cursor": "stalled",
        },
    )
    with pytest.raises(ChatGPTError, match="project discovery did not progress"):
        _discover(sidebar, RANGE, discovery_reporter())

    conversations = ProjectEndpointOverride(
        "conversations",
        {
            "items": [
                item(
                    "project-page",
                    "2026-01-01T00:20:00Z",
                    archived=False,
                    starred=False,
                    gizmo_id="g-p-project-one",
                )
            ],
            "cursor": "stalled",
        },
    )
    with pytest.raises(
        ChatGPTError, match="project conversation discovery did not progress"
    ):
        _discover(conversations, RANGE, discovery_reporter())


def test_project_discovery_rejects_malformed_index_item() -> None:
    api = ProjectDiscoveryAPI()
    api.project_sidebar_pages = [[{"gizmo": {}}]]

    with pytest.raises(ChatGPTError, match="project discovery item is invalid"):
        _discover(api, RANGE, discovery_reporter())


def test_project_discovery_rejects_non_project_gizmo_type() -> None:
    api = ProjectDiscoveryAPI()
    custom_gpt = project_item("g-synthetic-custom-gpt", "Synthetic custom GPT")
    custom_gpt["gizmo"]["gizmo"]["gizmo_type"] = "gpt"  # type: ignore[index]
    api.project_sidebar_pages = [[custom_gpt]]

    with pytest.raises(ChatGPTError, match="not a project"):
        _discover(api, RANGE, discovery_reporter())


def test_project_discovery_requires_explicit_pagination_end() -> None:
    sidebar = ProjectEndpointOverride(
        "sidebar",
        {"items": [project_item("g-p-project-one", "Synthetic project")]},
    )
    with pytest.raises(
        ChatGPTError, match="project discovery pagination response was invalid"
    ):
        _discover(sidebar, RANGE, discovery_reporter())

    conversations = ProjectEndpointOverride("conversations", {"items": []})
    with pytest.raises(
        ChatGPTError,
        match="project conversation discovery pagination response was invalid",
    ):
        _discover(conversations, RANGE, discovery_reporter())


class MovingProjectAPI(DiscoveryAPI):
    """Serve a different membership snapshot at the start of each pass."""

    def __init__(self, memberships: list[dict[str, str | None]]) -> None:
        super().__init__()
        self.memberships = memberships
        self.ordinary_passes = 0
        self.project_sidebar_pages = [
            [
                project_item("g-p-alpha", "Alpha project"),
                project_item("g-p-beta", "Beta project"),
            ]
        ]

    def request(self, endpoint: str, params: dict[str, str]) -> _Response:
        if self._begin_ordinary_pass(endpoint, params):
            self.ordinary_passes += 1
            self._apply_membership()
        return super().request(endpoint, params)

    def _apply_membership(self) -> None:
        snapshot = self.memberships[
            min(self.ordinary_passes, len(self.memberships)) - 1
        ]
        self._reset_pages()
        self.project_conversation_pages = {
            "g-p-alpha": [[]],
            "g-p-beta": [[]],
        }
        for source_id, project_id in snapshot.items():
            entry = item(
                source_id,
                "2026-01-01T00:30:00Z",
                archived=False,
                starred=False,
                gizmo_id=project_id,
            )
            if project_id is None:
                self.pages[(False, False)].append(entry)
            else:
                self.project_conversation_pages[project_id][0].append(entry)


def test_discovery_tracks_conversation_moved_into_project() -> None:
    api = MovingProjectAPI([{"mover": None}, {"mover": "g-p-alpha"}])

    candidates, _coverage, passes, _project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    assert passes == 3
    assert candidates["mover"].project is not None
    assert candidates["mover"].project.source_id == "g-p-alpha"
    assert {entry.get("kind") for entry in candidates["mover"].discovery_entries} == {
        None,
        "project",
    }


def test_discovery_clears_project_membership_when_conversation_moves_out() -> None:
    api = MovingProjectAPI([{"mover": "g-p-alpha"}, {"mover": None}])

    candidates, _coverage, passes, _project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    assert passes == 3
    assert candidates["mover"].project is None
    entries = candidates["mover"].discovery_entries
    assert {entry.get("kind") for entry in entries} == {None, "project"}
    assert {
        entry["project_id"] for entry in entries if entry.get("kind") == "project"
    } == {"g-p-alpha"}


def test_discovery_recovers_conversation_moved_between_listings() -> None:
    class MidPassMoveAPI(ProjectDiscoveryAPI):
        """Miss a conversation in pass 1, then expose it as ordinary history."""

        def __init__(self) -> None:
            super().__init__()
            self.ordinary_passes = 0
            self.project_sidebar_pages = [[project_item("g-p-alpha", "Alpha project")]]
            self.project_conversation_pages = {"g-p-alpha": [[]]}

        def request(self, endpoint: str, params: dict[str, str]) -> _Response:
            if self._begin_ordinary_pass(endpoint, params):
                self.ordinary_passes += 1
                self._reset_pages()
                self.project_conversation_pages = {"g-p-alpha": [[]]}
                if self.ordinary_passes > 1:
                    self.pages[(False, False)].append(
                        item(
                            "mover",
                            "2026-01-01T00:30:00Z",
                            archived=False,
                            starred=False,
                        )
                    )
            return super().request(endpoint, params)

    candidates, _coverage, passes, _project_coverage = _discover(
        MidPassMoveAPI(), RANGE, discovery_reporter()
    )

    assert passes == 3
    assert candidates["mover"].project is None


def test_discovery_uses_latest_project_when_conversation_moves_between_projects() -> (
    None
):
    api = MovingProjectAPI([{"mover": "g-p-alpha"}, {"mover": "g-p-beta"}])

    candidates, _coverage, passes, _project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    assert passes == 3
    assert candidates["mover"].project is not None
    assert candidates["mover"].project.source_id == "g-p-beta"
    assert {
        entry["project_id"]
        for entry in candidates["mover"].discovery_entries
        if entry.get("kind") == "project"
    } == {"g-p-alpha", "g-p-beta"}


def test_project_discovery_deduplicates_repeated_observations() -> None:
    api = ProjectDiscoveryAPI()

    candidates, _coverage, passes, _project_coverage = _discover(
        api, RANGE, discovery_reporter()
    )

    candidate = candidates["active-conversation"]
    assert candidate.project is not None
    assert candidate.project.source_id == "g-p-project-one"
    project_entries = [
        entry for entry in candidate.discovery_entries if entry.get("kind") == "project"
    ]
    assert len(project_entries) == 1
    assert project_entries[0]["project_id"] == "g-p-project-one"
    ordinary_entries = [
        entry for entry in candidate.discovery_entries if entry.get("kind") is None
    ]
    assert len(ordinary_entries) == 2 * passes


class HydrationAPI:
    account_id = "account-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def request(self, endpoint: str, params: dict[str, str]) -> _Response:
        self.calls.append((endpoint, params))
        if endpoint.endswith("/messages"):
            if params["before"] == "cursor-1":
                return response(
                    {
                        "messages": [{"id": "old-1"}],
                        "page_info": {
                            "start_cursor": "cursor-2",
                            "has_previous_page": True,
                            "has_next_page": True,
                        },
                    }
                )
            return response(
                {
                    "messages": [{"id": "old-2"}],
                    "page_info": {
                        "start_cursor": None,
                        "has_previous_page": False,
                        "has_next_page": True,
                    },
                }
            )
        return response(
            {
                "messages": [{"id": "current"}],
                "page_info": {
                    "start_cursor": "cursor-1",
                    "has_previous_page": True,
                    "has_next_page": False,
                },
            }
        )

    @staticmethod
    def json(value: _Response) -> dict[str, object] | list[object]:
        return json.loads(value.body)


def test_hydration_preserves_detail_and_all_older_raw_pages(tmp_path: Path) -> None:
    run = CollectionRun(
        Archive(tmp_path / "archive"),
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )
    api = HydrationAPI()
    candidate = _Candidate("conversation/1", RANGE.start, ())

    _hydrate(run, api, candidate)
    published = run.publish({"selected_conversations": 1})
    snapshot = next((published / "snapshots/conversation").iterdir())
    manifest = json.loads((snapshot / "snapshot.json").read_text())

    assert [entry["path"] for entry in manifest["evidence_files"]] == [
        "conversation.json",
        "messages.001.json",
        "messages.002.json",
    ]
    assert (snapshot / "conversation.json").read_bytes() == json.dumps(
        {
            "messages": [{"id": "current"}],
            "page_info": {
                "start_cursor": "cursor-1",
                "has_previous_page": True,
                "has_next_page": False,
            },
        }
    ).encode()
    assert all(
        entry["request"]["hydration_id"]
        == manifest["evidence_files"][0]["request"]["hydration_id"]
        for entry in manifest["evidence_files"]
    )
    assert [call[1].get("before") for call in api.calls] == [
        None,
        "cursor-1",
        "cursor-2",
    ]


def test_empty_discovery_publishes_an_empty_successful_run(tmp_path: Path) -> None:
    api = DiscoveryAPI()
    api.pages = {key: [] for key in api.pages}
    run = CollectionRun(
        Archive(tmp_path / "archive"),
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )

    result = _collect_api(run, reporter(), api)
    published = run.publish(result.coverage)

    assert json.loads((published / "run.json").read_text())["snapshots"] == []
    assert result.coverage["selected_conversations"] == 0
    assert (
        "Discovery update_time is validated for message/edit content changes but is "
        "not known to advance for every conversation metadata mutation."
        in result.coverage["source_limitations"]
    )


def test_provider_failure_leaves_staging_and_never_publishes(tmp_path: Path) -> None:
    class FailingAPI(DiscoveryAPI):
        def request(self, endpoint: str, params: dict[str, str]) -> _Response:
            raise ChatGPTError("ChatGPT request failed after retries: status=429")

    archive = Archive(tmp_path / "archive")
    run = CollectionRun(
        archive,
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )

    with pytest.raises(ChatGPTError, match="429"):
        _collect_api(run, reporter(), FailingAPI())

    assert run.staging.exists()
    assert not (archive.root / "runs").exists()


class Page:
    def __init__(self, status: int, body: str = "{}") -> None:
        self.status = status
        self.body = body
        self.calls = 0

    def evaluate(self, _expression: str, _arg: object) -> dict[str, object]:
        self.calls += 1
        return {"status": self.status, "headers": {}, "body": self.body}


class ProbeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._body = json.dumps(body).encode()

    def body(self) -> bytes:
        return self._body


class ProbeRequest:
    def __init__(self, responses: list[ProbeResponse | Exception]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> ProbeResponse:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ProbeContext:
    def __init__(self, request: ProbeRequest) -> None:
        self.request = request


class AuthPage:
    def __init__(self, *, url: str = "https://chatgpt.com"):
        self.url = url

    def goto(self, _url: str, **_kwargs: object) -> None:
        pass

    def evaluate(self, _expression: str, _arg: object) -> dict[str, object]:
        raise AssertionError("auth must not use page.evaluate")


class AuthBrowser:
    def __init__(self, context: ProbeContext, page: AuthPage):
        self.context = context
        self.page = page

    def __enter__(self) -> AuthBrowser:
        return self

    def __exit__(self, *_: object) -> None:
        pass


def session_response(
    *, email: str | None = "foo@example.com", name: str | None = "Foo"
) -> ProbeResponse:
    account = {"id": "account-1"}
    if email is not None:
        account["email"] = email
    if name is not None:
        account["name"] = name
    return ProbeResponse(200, {"accessToken": "must-not-leak", "account": account})


@pytest.mark.parametrize("missing_automation_flag", [False, True])
def test_collect_hydrates_ordinary_conversations_with_optional_automation_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_automation_flag: bool,
) -> None:
    discovery = DiscoveryAPI()
    hydration = HydrationAPI()
    if missing_automation_flag:
        for partition in discovery.pages.values():
            for entry in partition:
                if entry["is_automation_conversation"] is False:
                    del entry["is_automation_conversation"]

    class CollectionPage(AuthPage):
        def evaluate(self, _expression: str, _arg: object) -> dict[str, object]:
            assert isinstance(_arg, dict)
            url = urlsplit(_arg["url"])
            params = dict(parse_qsl(url.query))
            if url.path == "/api/auth/session":
                result = response(
                    {"accessToken": "synthetic-token", "account": {"id": "account-1"}}
                )
            elif url.path == "/backend-api/conversations" or url.path.startswith(
                "/backend-api/gizmos/"
            ):
                result = discovery.request(url.path, params)
            else:
                result = hydration.request(url.path, params)
            return {
                "status": result.status,
                "headers": {},
                "body": result.body.decode(),
            }

    browser = AuthBrowser(ProbeContext(ProbeRequest([])), CollectionPage())
    monkeypatch.setattr("tracebase.chatgpt._Browser", lambda *_args, **_kwargs: browser)
    run = CollectionRun(
        Archive(tmp_path / "archive"),
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )

    result = collect(run, reporter())
    published = run.publish(result.coverage)

    assert result.coverage["selected_conversations"] == 3
    assert run.snapshot_count == 3
    for snapshot in (published / "snapshots/conversation").iterdir():
        assert json.loads((snapshot / "conversation.json").read_text())["messages"] == [
            {"id": "current"}
        ]
    assert {
        endpoint
        for endpoint, _params in hydration.calls
        if not endpoint.endswith("/messages")
    } == {
        "/backend-api/conversations/active-conversation",
        "/backend-api/conversations/at-start",
        "/backend-api/conversations/archived-conversation",
    }


class ProjectCollectionAPI(ProjectDiscoveryAPI):
    def __init__(self) -> None:
        super().__init__()
        self.hydration = HydrationAPI()

    def request(self, endpoint: str, params: dict[str, str]) -> _Response:
        if endpoint == "/backend-api/conversations" or endpoint.startswith(
            "/backend-api/gizmos/"
        ):
            return super().request(endpoint, params)
        return self.hydration.request(endpoint, params)


def test_project_collection_records_membership_and_deduplicates(
    tmp_path: Path,
) -> None:
    run = CollectionRun(
        Archive(tmp_path / "archive"),
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )

    result = _collect_api(run, reporter(), ProjectCollectionAPI())
    published = run.publish(result.coverage)

    assert result.coverage["selected_conversations"] == 7
    assert result.coverage["selected_ordinary_conversations"] == 2
    assert result.coverage["selected_project_conversations"] == 5
    assert result.coverage["project_discovery"]["listed_projects"] == 2
    snapshots = {
        json.loads((snapshot / "snapshot.json").read_text())["source_id"]: json.loads(
            (snapshot / "snapshot.json").read_text()
        )
        for snapshot in (published / "snapshots/conversation").iterdir()
    }
    assert set(snapshots) == {
        "active-conversation",
        "at-start",
        "archived-conversation",
        "project-only",
        "project-conversation",
        "second-project-conversation",
        "project-at-start",
    }
    assert "custom-gpt-conversation" not in snapshots

    project_snapshot = snapshots["project-only"]["metadata"]
    assert project_snapshot["project"] == {
        "source_id": "g-p-project-one",
        "name": "Synthetic project",
    }
    provenance = snapshots["project-only"]["selection_provenance"][0]
    assert provenance["project_membership"] == {
        "source_id": "g-p-project-one",
        "name": "Synthetic project",
    }
    assert any(
        entry.get("kind") == "project" for entry in provenance["discovery_entries"]
    )
    moved = snapshots["active-conversation"]["selection_provenance"][0]
    assert moved["project_membership"]["source_id"] == "g-p-project-one"
    assert {entry.get("kind") for entry in moved["discovery_entries"]} == {
        None,
        "project",
    }
    assert snapshots["archived-conversation"]["metadata"]["project"] is None


def assert_collection_fails(tmp_path: Path, api: object, match: str) -> None:
    archive = Archive(tmp_path / "archive")
    run = CollectionRun(
        archive,
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )

    with pytest.raises(ChatGPTError, match=match):
        _collect_api(run, reporter(), api)  # type: ignore[arg-type]

    assert run.staging.exists()
    assert not (archive.root / "runs").exists()


def test_project_failure_leaves_staging_and_never_publishes(tmp_path: Path) -> None:
    class FailingProjects(ProjectCollectionAPI):
        def request(self, endpoint: str, params: dict[str, str]) -> _Response:
            if endpoint == "/backend-api/gizmos/snorlax/sidebar":
                raise ChatGPTError("ChatGPT project index failed")
            return super().request(endpoint, params)

    assert_collection_fails(tmp_path, FailingProjects(), "project index failed")


def test_project_hydration_failure_leaves_staging_and_never_publishes(
    tmp_path: Path,
) -> None:
    class BrokenHydration(ProjectCollectionAPI):
        def request(self, endpoint: str, params: dict[str, str]) -> _Response:
            if endpoint.startswith("/backend-api/conversations/"):
                self.hydration.calls.append((endpoint, params))
                return response(
                    {
                        "messages": [{"id": "current"}],
                        "page_info": {
                            "start_cursor": None,
                            "has_previous_page": True,
                        },
                    }
                )
            return super().request(endpoint, params)

    assert_collection_fails(tmp_path, BrokenHydration(), "pagination did not progress")


def test_auth_probe_uses_context_request_and_safe_identity() -> None:
    request = ProbeRequest([session_response()])

    result = _ChatGPTSessionProbe(ProbeContext(request)).check()

    assert result.session == _Session("account-1", "foo@example.com", "Foo")
    assert request.urls == ["https://chatgpt.com/api/auth/session"]
    assert "must-not-leak" not in repr(result)


def test_auth_probe_reports_unauthenticated_without_page_transport() -> None:
    result = _ChatGPTSessionProbe(
        ProbeContext(ProbeRequest([ProbeResponse(401, {})]))
    ).check()

    assert result.session is None


def test_auth_probe_rejects_malformed_provider_response() -> None:
    with pytest.raises(ChatGPTError, match="response is invalid"):
        _ChatGPTSessionProbe(
            ProbeContext(ProbeRequest([ProbeResponse(200, ["invalid"])]))
        ).check()


def test_authenticate_launches_uncontrolled_system_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "profile"
    command: list[str] = []

    class FinishedProcess:
        def wait(self) -> int:
            return 0

    def fake_popen(args: list[str]) -> FinishedProcess:
        command.extend(args)
        return FinishedProcess()

    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)
    monkeypatch.setattr(
        "tracebase.chatgpt._system_chromium_path", lambda: "/usr/bin/chromium"
    )
    monkeypatch.setattr("tracebase.chatgpt.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "tracebase.chatgpt.status",
        lambda: _Session("account-1", "foo@example.com"),
    )

    authenticate()

    assert command == [
        "/usr/bin/chromium",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://chatgpt.com",
    ]
    output = capsys.readouterr().out
    assert "ChatGPT browser opened" in output
    assert "Authenticated as foo@example.com" in output


def test_authenticate_reports_browser_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: tmp_path / "profile")
    monkeypatch.setattr(
        "tracebase.chatgpt.subprocess.Popen",
        lambda _args: (_ for _ in ()).throw(OSError("synthetic launch failure")),
    )

    with pytest.raises(ChatGPTError, match="could not be started"):
        authenticate()


def test_authenticate_waits_for_child_cleanup_before_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    calls: list[object] = []

    class InterruptedProcess:
        wait_calls = 0

        def wait(self, **kwargs: object) -> int:
            calls.append(("wait", kwargs))
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            if self.wait_calls == 2:
                raise subprocess.TimeoutExpired("chromium", kwargs["timeout"])
            return 0

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)
    monkeypatch.setattr("tracebase.chatgpt._system_chromium_path", lambda: "chromium")
    monkeypatch.setattr(
        "tracebase.chatgpt.subprocess.Popen", lambda _args: InterruptedProcess()
    )

    with pytest.raises(ChatGPTAuthenticationError, match="cancelled"):
        authenticate()

    assert calls == [
        ("wait", {}),
        "terminate",
        ("wait", {"timeout": 5}),
        "kill",
        ("wait", {}),
    ]
    with _ProfileLock(profile):
        pass


def test_auth_probe_identity_fallbacks() -> None:
    for body, label in (
        ({"accessToken": "token", "account": {"id": "id", "email": "e"}}, "e"),
        (
            {"accessToken": "token", "account": {"id": "id"}, "user": {"email": "u"}},
            "u",
        ),
        ({"accessToken": "token", "account": {"id": "id", "name": "N"}}, "N"),
        (
            {"accessToken": "token", "account": {"id": "id"}, "user": {"name": "U"}},
            "U",
        ),
        ({"accessToken": "token", "account": {"id": "id"}}, "account id"),
        (
            {
                "accessToken": "token",
                "account": {"id": "id", "email": "", "name": ""},
                "user": {"email": "u", "name": "User"},
            },
            "u",
        ),
        (
            {
                "accessToken": "token",
                "account": {"id": "id", "email": "", "name": ""},
                "user": {"name": "User"},
            },
            "User",
        ),
    ):
        result = _ChatGPTSessionProbe(
            ProbeContext(ProbeRequest([ProbeResponse(200, body)]))
        ).check()
        assert result.session is not None
        assert result.session.label == label


def test_auth_session_derives_scope_without_exposing_token() -> None:
    token = "access-token-that-must-stay-in-memory"
    api = _ChatGPTAPI(
        Page(
            200,
            json.dumps({"accessToken": token, "account": {"id": "account-1"}}),
        )
    )

    session = api.session()

    assert session.account_id == "account-1"
    assert token not in repr(session)


def test_401_is_a_clean_auth_failure() -> None:
    api = _ChatGPTAPI(Page(401))

    with pytest.raises(ChatGPTAuthenticationError, match="expired"):
        api.session()


def test_explicit_browser_mode_is_propagated_to_context_and_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = ChatGPTContext("chatgpt", "account-1", "test", {})
    context_modes: list[str] = []
    collection_modes: list[str] = []
    result = CollectionResult(coverage={})

    monkeypatch.setattr(
        "tracebase.chatgpt._resolve_context_with_browser",
        lambda browser_mode: context_modes.append(browser_mode) or context,
    )
    monkeypatch.setattr(
        "tracebase.chatgpt._collect_with_browser",
        lambda run, reporter, browser_mode: (
            collection_modes.append(browser_mode) or result
        ),
    )

    assert resolve_context("headed") == context
    run = CollectionRun(
        Archive(tmp_path / "archive"),
        "chatgpt",
        "account-1",
        RANGE,
        collector_version="test",
        effective_options={},
    )
    assert collect(run, reporter(), "headed") is result
    assert context_modes == ["headed"]
    assert collection_modes == ["headed"]


def test_context_records_explicit_browser_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes: list[tuple[bool, bool]] = []

    class FakeBrowser:
        def __init__(self, profile: Path, *, headless: bool, stealth: bool) -> None:
            modes.append((headless, stealth))
            self.page = Page(
                200,
                json.dumps(
                    {"accessToken": "test-token", "account": {"id": "account-1"}}
                ),
            )

        def __enter__(self) -> FakeBrowser:
            return self

        def __exit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr("tracebase.chatgpt._Browser", FakeBrowser)

    context = resolve_context("headed")

    assert modes == [(False, False)]
    assert context.effective_options["browser_mode"] == "headed"
    assert context.effective_options["browser_execution"] == "native-headed"


def test_browser_verification_does_not_fallback_from_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail(browser_mode: str) -> ChatGPTContext:
        calls.append(browser_mode)
        raise ChatGPTBrowserVerificationError("verification required")

    monkeypatch.setattr("tracebase.chatgpt._resolve_context_with_browser", fail)

    with pytest.raises(ChatGPTBrowserVerificationError, match="verification"):
        resolve_context("headless")

    assert calls == ["headless"]


def test_profile_path_uses_platform_state_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracebase.chatgpt.platform.system", lambda: "Darwin")
    monkeypatch.setattr("tracebase.chatgpt.Path.home", lambda: Path("/home/tester"))
    assert profile_path() == Path(
        "/home/tester/Library/Application Support/tracebase/chatgpt-browser"
    )

    monkeypatch.setattr("tracebase.chatgpt.platform.system", lambda: "Linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert profile_path() == Path("/home/tester/.local/state/tracebase/chatgpt-browser")

    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert profile_path() == Path("/home/tester/.local/state/tracebase/chatgpt-browser")

    monkeypatch.setenv("XDG_STATE_HOME", "relative/path")
    assert profile_path() == Path("/home/tester/.local/state/tracebase/chatgpt-browser")

    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/tester-state")
    assert profile_path() == Path("/tmp/tester-state/tracebase/chatgpt-browser")


def test_windows_profile_path_rejects_empty_or_relative_localappdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tracebase.chatgpt.platform.system", lambda: "Windows")
    monkeypatch.setattr("tracebase.chatgpt.Path.home", lambda: Path("/home/tester"))

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert profile_path() == Path(
        "/home/tester/AppData/Local/tracebase/chatgpt-browser"
    )

    monkeypatch.setenv("LOCALAPPDATA", "relative/path")
    assert profile_path() == Path(
        "/home/tester/AppData/Local/tracebase/chatgpt-browser"
    )

    monkeypatch.setenv("LOCALAPPDATA", "")
    assert profile_path() == Path(
        "/home/tester/AppData/Local/tracebase/chatgpt-browser"
    )


def test_profile_lock_rejects_concurrent_use(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    second = _ProfileLock(profile)
    with _ProfileLock(profile), pytest.raises(ArchiveError, match="already in use"):
        second.__enter__()


def test_profile_lock_restricts_existing_profile_permissions(tmp_path: Path) -> None:
    profile = tmp_path / "tracebase" / "profile"
    profile.mkdir(parents=True)
    profile.parent.chmod(0o755)
    profile.chmod(0o755)

    with _ProfileLock(profile):
        assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        assert stat.S_IMODE(profile.with_name("profile.lock").stat().st_mode) == 0o600


def test_browser_launch_failure_releases_profile_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingPlaywright:
        def start(self) -> None:
            raise RuntimeError("launch failed")

    monkeypatch.setattr("tracebase.chatgpt.sync_playwright", FailingPlaywright)
    profile = tmp_path / "profile"

    with (
        pytest.raises(ChatGPTError, match="could not be started"),
        _Browser(profile, headless=True, stealth=False),
    ):
        pass

    with _ProfileLock(profile):
        pass


def test_browser_uses_the_system_chromium_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_options: dict[str, object] = {}

    class FakePage:
        def goto(self, _url: str, **_kwargs: object) -> None:
            pass

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        def new_page(self) -> FakePage:
            return self.pages[0]

        def close(self) -> None:
            pass

    class FakeChromium:
        def launch_persistent_context(self, **kwargs: object) -> FakeContext:
            launch_options.update(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        def start(self) -> FakePlaywright:
            return self

        def stop(self) -> None:
            pass

    monkeypatch.setattr("tracebase.chatgpt.sync_playwright", FakePlaywright)
    monkeypatch.setattr(
        "tracebase.chatgpt._system_chromium_path", lambda: "/usr/bin/chromium"
    )

    with _Browser(tmp_path / "profile", headless=False, stealth=False):
        pass

    assert "channel" not in launch_options
    assert launch_options["headless"] is False
    assert launch_options["executable_path"] == "/usr/bin/chromium"


def test_cli_registers_chatgpt_collection_and_auth_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect_args = _parser().parse_args(
        [
            "collect",
            "chatgpt",
            "--archive",
            "/tmp/archive",
            "--from",
            "2026-01-01T00:00:00Z",
            "--to",
            "2026-01-01T01:00:00Z",
        ]
    )
    auth_args = _parser().parse_args(["auth", "chatgpt"])
    status_args = _parser().parse_args(["auth", "chatgpt", "status"])
    reset_args = _parser().parse_args(["auth", "chatgpt", "reset"])

    assert collect_args.source == "chatgpt"
    assert collect_args.browser_mode == "headless"
    assert auth_args.command == "auth"
    assert auth_args.auth_action is None
    assert status_args.auth_action == "status"
    assert reset_args.auth_action == "reset"
    headed_args = _parser().parse_args(
        [
            "collect",
            "chatgpt",
            "--archive",
            "/tmp/archive",
            "--browser-mode",
            "headed",
            "--from",
            "2026-01-01T00:00:00Z",
            "--to",
            "2026-01-01T01:00:00Z",
        ]
    )
    assert headed_args.browser_mode == "headed"
    monkeypatch.setattr("tracebase.cli.authenticate_chatgpt", lambda: None)
    assert main(["auth", "chatgpt"]) == 0


def test_cli_chatgpt_auth_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracebase.cli.status_chatgpt", lambda: None)
    assert main(["auth", "chatgpt", "status"]) == 1

    calls: list[str] = []
    monkeypatch.setattr("tracebase.cli.reset_chatgpt", lambda: calls.append("reset"))
    assert main(["auth", "chatgpt", "reset"]) == 0
    assert calls == ["reset"]


def test_cli_status_authenticated_is_headless_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "tracebase.cli.status_chatgpt",
        lambda: _Session("id", "foo@example.com"),
    )

    assert main(["auth", "chatgpt", "status"]) == 0
    assert capsys.readouterr().out == "Authenticated as foo@example.com\n"


def test_status_uses_headless_fake_browser_for_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "chatgpt-browser"
    profile.mkdir()
    request = ProbeRequest([session_response()])
    browser = AuthBrowser(ProbeContext(request), AuthPage())
    modes: list[bool] = []
    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)

    def fake_browser(*_args: object, **kwargs: object) -> AuthBrowser:
        modes.append(bool(kwargs["headless"]))
        return browser

    monkeypatch.setattr("tracebase.chatgpt._Browser", fake_browser)

    result = status()

    assert result is not None
    assert result.label == "foo@example.com"
    assert modes == [True]


def test_cli_status_reports_probe_error_without_not_authenticated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "tracebase.cli.status_chatgpt",
        lambda: (_ for _ in ()).throw(ChatGPTError("session probe failed")),
    )

    assert main(["auth", "chatgpt", "status"]) == 1
    output = capsys.readouterr()
    assert output.err == "session probe failed\n"
    assert output.out == ""


def test_cli_reset_reports_deletion_failure_without_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "tracebase.cli.reset_chatgpt",
        lambda: (_ for _ in ()).throw(
            ChatGPTError("ChatGPT browser profile could not be reset")
        ),
    )

    assert main(["auth", "chatgpt", "reset"]) == 1
    output = capsys.readouterr()
    assert output.err == "ChatGPT browser profile could not be reset\n"
    assert output.out == ""


def test_reset_reports_filesystem_deletion_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "chatgpt-browser"
    profile.mkdir()
    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)

    def fail_rmtree(_path: Path) -> None:
        raise OSError("synthetic deletion failure")

    monkeypatch.setattr("tracebase.chatgpt.shutil.rmtree", fail_rmtree)

    with pytest.raises(ChatGPTError, match="could not be reset"):
        reset()


def test_cli_rejects_unsupported_chatgpt_auth_action() -> None:
    with pytest.raises(ArchiveError, match="invalid command arguments"):
        _parser().parse_args(["auth", "chatgpt", "logout"])


def test_status_does_not_create_absent_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = Path("/tmp/synthetic-chatgpt-profile")
    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)
    assert status() is None


def test_reset_is_idempotent_and_keeps_unrelated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "chatgpt-browser"
    (profile / "nested").mkdir(parents=True)
    (profile / "nested" / "state").write_text("synthetic")
    unrelated = tmp_path / "archive" / "runs" / "keep.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep")
    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)

    reset()
    reset()

    assert not profile.exists()
    assert unrelated.read_text() == "keep"


def test_reset_refuses_locked_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "chatgpt-browser"
    profile.mkdir()
    monkeypatch.setattr("tracebase.chatgpt.profile_path", lambda: profile)

    with _ProfileLock(profile), pytest.raises(ArchiveError, match="already in use"):
        reset()
    assert profile.exists()


def test_cli_passes_browser_mode_to_chatgpt_collector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = ChatGPTContext("chatgpt", "account-1", "test", {})
    context_modes: list[str] = []
    collection_modes: list[str] = []

    monkeypatch.setattr(
        "tracebase.cli.resolve_chatgpt_context",
        lambda browser_mode: context_modes.append(browser_mode) or context,
    )
    monkeypatch.setattr(
        "tracebase.cli.collect_chatgpt",
        lambda run, progress, browser_mode: (
            collection_modes.append(browser_mode) or CollectionResult(coverage={})
        ),
    )

    assert (
        main(
            [
                "collect",
                "chatgpt",
                "--archive",
                str(tmp_path / "archive"),
                "--browser-mode",
                "headed",
                "--from",
                "2026-01-01T00:00:00Z",
                "--to",
                "2026-01-01T01:00:00Z",
            ]
        )
        == 0
    )
    assert context_modes == ["headed"]
    assert collection_modes == ["headed"]
