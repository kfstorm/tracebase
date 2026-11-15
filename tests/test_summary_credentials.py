"""Summary credential tests use synthetic CLI responses and credentials only."""

import json
import subprocess
from io import BytesIO

import pytest

from tests.test_summary import FakeRunner, context
from tracebase.summary import SummaryError, SummaryRequest, summarize
from tracebase.summary_opencode import prepare_state

CREDENTIALS = (
    b'[{"id":"cred_synthetic","integrationID":"synthetic","label":"Synthetic key",'
    b'"active":true,"value":{"type":"key","key":"synthetic-secret"}}]'
)


def environment(tmp_path, monkeypatch, host_version, *, fail=""):
    calls = []
    data = tmp_path / "host-data"
    auth = data / "opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"example":{"type":"api","key":"synthetic-secret"}}')
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(
        "tracebase.summary_opencode.shutil.which", lambda _: "/synthetic/bin/opencode"
    )

    def execute(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments == ["/synthetic/bin/opencode", "--version"]:
            return subprocess.CompletedProcess(arguments, 0, host_version, b"")
        if arguments[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, b"", b"")
        operation = "export" if arguments[-1] == "export" else "import"
        assert operation == "export" or arguments[:2] == ["docker", "run"]
        return subprocess.CompletedProcess(
            arguments, int(fail == operation), CREDENTIALS, CREDENTIALS
        )

    monkeypatch.setattr("tracebase.summary_opencode.subprocess.run", execute)
    monkeypatch.setattr(
        "tracebase.summary.ContainerRunner",
        lambda _image, mounts, **_kwargs: FakeRunner(mounts.work, mounts.results),
    )
    return calls, auth


@pytest.mark.parametrize(
    "host_version,container_version",
    [
        (b"1.19.5\n", "1.99.1"),
        (b"opencode v2.3.0\n", "2.99.1"),
        (b"1.19.5\n", "latest"),
        (b"opencode v2.3.0\n", "latest"),
    ],
)
def test_matching_major_initializes_private_credentials(
    tmp_path, monkeypatch, host_version, container_version
):
    source = context(tmp_path)
    calls, auth = environment(tmp_path, monkeypatch, host_version)
    monkeypatch.setattr(
        "tracebase.summary.urlopen",
        lambda *args, **kwargs: BytesIO(
            b'[{"tag_name":"v2.99.1","draft":false,"prerelease":false},'
            b'{"tag_name":"v1.99.1","draft":false,"prerelease":false}]'
        ),
    )
    state_seen = []

    def prepare(state, host, image):
        prepare_state(state, host, image)
        state_seen.append(state)
        assert state.stat().st_mode & 0o777 == 0o700
        if host.major == 1:
            isolated = state / "share/opencode/auth.json"
            assert isolated.stat().st_mode & 0o777 == 0o600
            assert json.loads(isolated.read_text()) == json.loads(auth.read_text())

    monkeypatch.setattr("tracebase.summary.prepare_state", prepare)
    summarize(
        SummaryRequest(
            source,
            "model",
            None,
            tmp_path / "output",
            opencode_version=container_version,
        )
    )
    assert all(not state.exists() for state in state_seen)
    expected_major = 2 if host_version.startswith(b"opencode v2.") else 1
    assert calls[1][0] == [
        "docker",
        "image",
        "inspect",
        f"tracebase-opencode-summary:{expected_major}.99.1",
    ]
    if host_version.startswith(b"opencode v2."):
        export = next(call for call in calls if call[0][-1] == "export")
        assert export[0] == [
            "/synthetic/bin/opencode",
            "--standalone",
            "auth",
            "export",
        ]
        imported = next(call for call in calls if call[0][-1] == "import")
        assert imported[1]["input"] == CREDENTIALS
        assert imported[1]["stdout"] == subprocess.DEVNULL
        assert imported[1]["stderr"] == subprocess.DEVNULL
        assert "--network" in imported[0] and "none" in imported[0]
        assert any(str(state_seen[0]) in arg for arg in imported[0])
        assert CREDENTIALS.decode() not in " ".join(imported[0])
    else:
        assert len(calls) == 2  # version detection and image lookup only


@pytest.mark.parametrize(
    "host_version,container_version",
    [
        (b"1.19.5", "2.3.0"),
        (b"opencode v2.3.0", "1.19.5"),
        (b"unparseable synthetic-secret", "1.19.5"),
        (b"3.0.0", "1.19.5"),
        (b"1.19.5", "3.0.0"),
    ],
)
def test_incompatible_or_invalid_versions_fail_before_credentials_and_docker(
    tmp_path, monkeypatch, host_version, container_version
):
    source = context(tmp_path)
    calls, _ = environment(tmp_path, monkeypatch, host_version)
    monkeypatch.setattr(
        "tracebase.summary_opencode._auth_path",
        lambda: pytest.fail("credentials accessed"),
    )
    with pytest.raises(SummaryError) as caught:
        summarize(
            SummaryRequest(
                source,
                "model",
                None,
                tmp_path / "output",
                opencode_version=container_version,
            )
        )
    assert len(calls) == 1
    assert "synthetic-secret" not in str(caught.value)
    if host_version in {b"1.19.5", b"opencode v2.3.0"}:
        assert "invalid OpenCode version selection" in str(caught.value)
    assert not list(tmp_path.glob(".summary-run.*"))


@pytest.mark.parametrize("operation", ["export", "import"])
def test_v2_credential_failure_stops_summary_and_keeps_secrets_out_of_debug(
    tmp_path, monkeypatch, operation
):
    source = context(tmp_path)
    calls, _ = environment(tmp_path, monkeypatch, b"opencode v2.3.0", fail=operation)
    monkeypatch.setattr(
        "tracebase.summary.ContainerRunner",
        lambda *_args, **_kwargs: pytest.fail("Summary started after auth failure"),
    )
    debug = tmp_path / "debug"
    with pytest.raises(SummaryError, match=f"credential {operation} failed") as caught:
        summarize(
            SummaryRequest(
                source,
                "model",
                None,
                tmp_path / "output",
                opencode_version="2.99.0",
                debug_output=debug,
            )
        )
    assert "synthetic-secret" not in str(caught.value)
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".summary-run.*"))
    assert not list(debug.rglob("auth.json"))
    assert not list(debug.rglob("*.db"))
    assert all(
        b"synthetic-secret" not in path.read_bytes()
        for path in debug.rglob("*")
        if path.is_file()
    )
    assert sum(call[0][-1] == "import" for call in calls) == (operation == "import")


@pytest.mark.parametrize("mode", ["missing", "error", "exit", "encoding"])
def test_host_version_detection_failure_is_explicit(tmp_path, monkeypatch, mode):
    source = context(tmp_path)
    environment(tmp_path, monkeypatch, b"\xff" if mode == "encoding" else b"1.19.5")
    if mode == "missing":
        monkeypatch.setattr("tracebase.summary_opencode.shutil.which", lambda _: None)
    elif mode in {"error", "exit"}:

        def execute(*args, **kwargs):
            if mode == "error":
                raise OSError("synthetic-secret")
            return subprocess.CompletedProcess([], 1, CREDENTIALS, CREDENTIALS)

        monkeypatch.setattr("tracebase.summary_opencode.subprocess.run", execute)
    with pytest.raises(SummaryError, match=r"host OpenCode|Host OpenCode") as caught:
        summarize(
            SummaryRequest(
                source, "model", None, tmp_path / "output", opencode_version="1.99.0"
            )
        )
    assert "synthetic-secret" not in str(caught.value)
