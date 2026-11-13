"""Provider diff limits preserve the remaining PR evidence and explain the gap."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_cli import (
    build_github_pr_fixture,
    build_github_request,
    build_github_run,
    build_test_reporter,
    completed_gh,
    completed_github_response,
)
from tests.test_context_contracts import activity_evidence, github_output, text
from tracebase.archive import Archive, ArchiveError
from tracebase.github import _GitHub, _Response, collect

LIMIT_MESSAGE = "Sorry, the diff exceeded the maximum number of files (300)."


@pytest.mark.parametrize(
    ("status", "message", "allowed"),
    [
        (406, LIMIT_MESSAGE, True),
        (406, "Not Acceptable", False),
        (403, LIMIT_MESSAGE, False),
    ],
)
def test_collection_records_only_diff_limit_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    message: str,
    allowed: bool,
) -> None:
    item, responses = build_github_pr_fixture()
    base_request = build_github_request(item, responses)
    original_request = _GitHub.request
    error_body = json.dumps(
        {"message": message, "documentation_url": "https://example.com/api"}
    ).encode()

    def execute(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if arguments[-1] == "--help":
            return completed_gh(b"gh api help")
        return completed_github_response(
            status,
            error_body,
            returncode=1,
            stderr=f"gh: {message} (HTTP {status})".encode(),
        )

    def request(
        self: _GitHub, endpoint: str, accept: str = "application/vnd.github+json"
    ) -> _Response:
        if accept == "application/vnd.github.diff":
            return original_request(self, endpoint, accept)
        return base_request(self, endpoint, accept)

    monkeypatch.setattr("tracebase.github.subprocess.run", execute)
    monkeypatch.setattr("tracebase.github._GitHub.request", request)
    monkeypatch.setattr(
        "tracebase.github._GitHub.graphql",
        lambda *_args: _Response(
            b'{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}}}}}}',
            200,
            {},
        ),
    )
    run = build_github_run(Archive(tmp_path))
    if not allowed:
        with pytest.raises(ArchiveError) as error:
            collect(run, build_test_reporter())
        diagnostic = str(error.value)
        assert f"gh: {message} (HTTP {status})" in diagnostic
        assert "endpoint=/repos/octo/example/pulls/7" in diagnostic
        assert "returncode=1" in diagnostic
        assert f"status={status}" in diagnostic
        assert not list((tmp_path / "runs").glob("*/run.json"))
        return
    result = collect(run, build_test_reporter())
    published = run.publish(result.coverage)
    snapshot = next((published / "snapshots/pull-request").iterdir())
    assert (snapshot / "pull-request.diff-unavailable.json").read_bytes() == error_body
    assert not (snapshot / "pull-request.diff").exists()
    assert (snapshot / "pull-request.json").exists()
    manifest = json.loads((snapshot / "snapshot.json").read_text())
    failed_diff = next(
        entry
        for entry in manifest["evidence_files"]
        if entry["path"] == "pull-request.diff-unavailable.json"
    )
    assert failed_diff["request"]["response_status"] == 406
    assert failed_diff["request"]["accept"] == "application/vnd.github.diff"


def test_context_explains_unavailable_diff_without_creating_patch(
    tmp_path: Path,
) -> None:
    archive = Archive(tmp_path / "archive")
    archive.root.mkdir()
    output = github_output(
        archive,
        tmp_path,
        {
            **activity_evidence(),
            "pull-request.diff-unavailable.json": {"message": LIMIT_MESSAGE},
        },
    )
    overview = text(output, "overview.md")
    assert "Diff unavailable" in overview
    assert "HTTP 406" in overview
    assert LIMIT_MESSAGE in overview
    assert "do not infer" in overview.lower()
    assert not list(output.rglob("diff.patch"))
