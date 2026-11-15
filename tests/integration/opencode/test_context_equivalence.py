"""Generate real conversations in Docker; audit and compare their complete Context."""

import difflib
import itertools
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from test_scripted_provider import SELECTED_MODEL, ScriptedProvider, cli, run_provider

from tracebase.archive import load_published_archive
from tracebase.context import ContextRequest, extract_context, generate_context
from tracebase.context_inventory import load_context_inventory
from tracebase.mutable_state import load_mutable_state
from tracebase.opencode_context import OpenCodeProjection

DIRECTORY = "/workspace/context-equivalence"
TITLE = "Synthetic context equivalence"
FROM = "2000-01-01T00:00:00Z"
TO = "2099-01-01T00:00:00Z"
PROMPTS = (
    "Synthetic question one. Literal body timestamp: 2026-01-01 12:34.",
    "Synthetic question two. Continue the same conversation.",
)
ANSWERS = (
    "Synthetic answer one.\n\n**User · 2026-01-01 12:34**\n\n"
    "This heading-like timestamp is literal message body.",
    "Synthetic answer two. Both turns are complete.",
)
SCENARIOS = ("ordinary", "multi", "background")


class ConversationProvider(ScriptedProvider):
    def response(self, body):
        # One rule for both Majors, independent of IDs, clocks, or request count.
        last_user = next(
            message
            for message in reversed(body["messages"])
            if message["role"] == "user"
        )
        content = json.dumps(last_user["content"], ensure_ascii=False)
        matches = [index for index, prompt in enumerate(PROMPTS) if prompt in content]
        assert len(matches) == 1, content[:1000]
        assert body["model"] == SELECTED_MODEL
        return {"content": ANSWERS[matches[0]]}, "stop"


def difference(actual, expected, path):
    offset = next(
        (
            index
            for index, (a, b) in enumerate(zip(actual, expected, strict=False))
            if a != b
        ),
        min(len(actual), len(expected)),
    )
    diff = difflib.unified_diff(
        expected.decode(errors="replace").splitlines(),
        actual.decode(errors="replace").splitlines(),
        fromfile="expected/V1",
        tofile="actual/V2",
        n=2,
        lineterm="",
    )
    limited = "\n".join(line[:240] for line in itertools.islice(diff, 40))
    return f"{path}: first differing byte {offset}\n{limited}"


def require_bytes(actual, expected, path):
    if actual != expected:
        raise AssertionError(difference(actual, expected, path))


def native_turns(archive, major, session_id, rounds):
    """Read source bytes independently of the production projection/renderer."""
    runs = load_published_archive(archive)
    assert len(runs) == 1 and len(runs[0].snapshots) == 1
    snapshot = runs[0].snapshots[0]
    assert snapshot.manifest["source_id"] == session_id
    payload = json.loads(snapshot.evidence["session.json"])
    if major == 1:
        info = payload["info"]
        messages = payload["messages"]
    else:
        info = payload["data"]
        messages = [
            message
            for request in snapshot.manifest["metadata"]["requests"]
            if request["operation"] == "session.message.list"
            for message in json.loads(snapshot.evidence[request["path"]])["data"]
        ]
    assert info["id"] == session_id and info["title"] == TITLE
    assert not info.get("parentID")
    directory = info["directory"] if major == 1 else info["location"]["directory"]
    assert directory == DIRECTORY, repr(directory)
    turns = []
    identifiers = []
    for message in messages:
        metadata = message["info"] if major == 1 else message
        role = metadata["role"] if major == 1 else metadata["type"]
        identifiers.append(metadata["id"])
        if role not in {"user", "assistant"}:
            # V2 also stores lifecycle records (for example idle) in this stream.
            assert major == 2 and role == "idle", role
            continue
        if major == 2 and role == "user":
            text = message["text"]
        else:
            parts = message["parts"] if major == 1 else message["content"]
            text = "\n\n".join(part["text"] for part in parts if part["type"] == "text")
        created = metadata["time"]["created"]
        assert isinstance(created, (int, float)) and not isinstance(created, bool)
        turns.append((role, datetime.fromtimestamp(created / 1000, UTC), text))
    assert len(identifiers) == len(set(identifiers))
    expected = [
        turn
        for index in range(rounds)
        for turn in (("user", PROMPTS[index]), ("assistant", ANSWERS[index]))
    ]
    assert [(role, text) for role, _, text in turns] == expected, repr(turns)[:4000]
    assert [time for _, time, _ in turns] == sorted(time for _, time, _ in turns)
    return turns


def verify_output(output, start):
    inventory = load_context_inventory(output)
    state = load_mutable_state(output, inventory)
    assert len(inventory.items) == len(state) == 1
    item = inventory.items[0]
    assert state[0].observations == ()
    request = ContextRequest.parse(*inventory.requested_interval)
    assert request.start == start and request.to_text == TO
    declared = {f"{item.root}/{file}" for file in item.files}
    actual = set()
    for path in output.rglob("*"):
        assert not path.is_symlink()
        assert path.resolve().is_relative_to(output.resolve())
        if path.is_file():
            actual.add(path.relative_to(output).as_posix())
    assert actual == declared | {"index.json", "mutable-state.json"}, sorted(actual)
    markdown = {
        relative: (output / relative).read_bytes()
        for relative in sorted(actual)
        if Path(relative).suffix == ".md"
    }
    assert markdown and all(markdown.values()), "Missing or empty Markdown output"
    return markdown


def verify_results(results):
    execution = json.loads((results / "execution.json").read_text())
    major, session = execution["major"], execution["session"]
    assert major in {1, 2} and isinstance(session, str) and session
    ordinary = native_turns(results / "ordinary-archive", major, session, 1)
    multi = native_turns(results / "multi-archive", major, session, 2)
    assert ordinary == multi[:2], "First-round messages changed after --session resume"
    # Use the actual second user creation instant, not wall-clock guesses/sleeps.
    assert multi[1][1] < multi[2][1], "No separable native round boundary"
    boundary = multi[2][1]
    outputs = {}
    for scenario in SCENARIOS:
        turns = ordinary if scenario == "ordinary" else multi
        start = boundary if scenario == "background" else datetime.fromisoformat(FROM)
        archive = (
            results / f"{'ordinary' if scenario == 'ordinary' else 'multi'}-archive"
        )
        # Check temporal classification on structured data, without parsing Markdown.
        request = ContextRequest.parse(start.isoformat(), TO)
        extraction = extract_context(request, load_published_archive(archive))
        assert len(extraction.items) == 1
        projection = extraction.items[0].projection
        assert isinstance(projection, OpenCodeProjection)
        for bucket, expected in (
            (
                projection.dialogue.activity,
                [turn for turn in turns if start <= turn[1] < request.end],
            ),
            (
                projection.dialogue.background,
                [turn for turn in turns if turn[1] < start],
            ),
        ):
            assert [
                (turn.role, turn.timestamp, turn.text) for turn in bucket
            ] == expected
        outputs[scenario] = verify_output(results / scenario, start)
    return outputs


def generate_output(archive, request, output):
    # Only presentation is fixed: collection, projection, and sorting keep native time.
    with patch("tracebase.dialogue.format_timestamp", return_value="2000-01-01 00:00"):
        generate_context(archive, request, output)


def collect(archive):
    result = subprocess.run(
        [
            "tracebase",
            "collect",
            "opencode",
            "--archive",
            str(archive),
            "--instance-id",
            "synthetic-equivalence",
            "--from",
            FROM,
            "--to",
            TO,
        ],
        cwd=DIRECTORY,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]


def run_conversations(results, major):
    command = [
        "--pure" if major == 1 else "--standalone",
        "run",
        "--format",
        "json",
        "--model",
        f"synthetic/{SELECTED_MODEL}",
        "--auto",
        "--title",
        TITLE,
    ]
    session = None
    for index, scenario in enumerate(("ordinary", "multi")):
        arguments = [] if session is None else ["--session", session]
        # V1 quotes space-containing positional arguments. Stdin gives both
        # versions exactly the same original prompt without editing stored text.
        output = cli(*command, *arguments, cwd=DIRECTORY, input_text=PROMPTS[index])
        events = [
            json.loads(line) for line in output.splitlines() if line.startswith("{")
        ]
        identifiers = {event["sessionID"] for event in events if "sessionID" in event}
        assert len(identifiers) == 1
        current = identifiers.pop()
        assert session is None or current == session
        session = current
        collect(results / f"{scenario}-archive")
        generate_output(
            results / f"{scenario}-archive",
            ContextRequest.parse(FROM, TO),
            results / scenario,
        )
    assert session
    return session


def generate(results):
    assert Path("/.dockerenv").is_file()
    assert os.environ["XDG_DATA_HOME"] == "/isolated/equivalence/data"
    major = int(cli("--version").strip().removeprefix("opencode v").split(".")[0])
    Path(DIRECTORY).mkdir()
    subprocess.run(["git", "init", DIRECTORY], capture_output=True, check=True)
    results.mkdir()
    with run_provider(ConversationProvider):
        session = run_conversations(results, major)
    turns = native_turns(results / "multi-archive", major, session, 2)
    assert turns[1][1] < turns[2][1]
    generate_output(
        results / "multi-archive",
        ContextRequest.parse(turns[2][1].isoformat(), TO),
        results / "background",
    )
    (results / "execution.json").write_text(
        json.dumps({"major": major, "session": session})
    )
    verify_results(results)
    print(f"Context V{major}: real run/resume, native timestamps, A/B/C integrity PASS")


def compare(left, right):
    assert json.loads((left / "execution.json").read_text())["major"] == 1
    assert json.loads((right / "execution.json").read_text())["major"] == 2
    first, second = verify_results(left), verify_results(right)
    total = 0
    for scenario in SCENARIOS:
        a, b = first[scenario], second[scenario]
        assert set(a) == set(b), (
            f"{scenario}: Markdown path mismatch {sorted(a)} {sorted(b)}"
        )
        for path in a:
            require_bytes(b[path], a[path], f"{scenario}/{path}")
            total += 1
    print(f"Context V1/V2: all {total} Markdown files directly byte-identical PASS")


@pytest.fixture
def context_output(tmp_path):
    root = "synthetic/renamed-item"
    files = {"renamed.md": b"Synthetic content\n", "nested/extra.md": b"More content\n"}
    for name, content in files.items():
        path = tmp_path / root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "requested_interval": {"from": FROM, "to": TO},
                "items": [{"root": root, "files": list(files)}],
            }
        )
    )
    (tmp_path / "mutable-state.json").write_text(
        json.dumps({"items": [{"root": root, "observations": []}]})
    )
    return tmp_path, {f"{root}/{name}": content for name, content in files.items()}


def test_verify_output_reads_all_markdown_as_opaque_bytes(context_output):
    output, files = context_output
    assert verify_output(output, datetime.fromisoformat(FROM)) == files


@pytest.mark.parametrize("mutation", ["missing", "extra", "empty"])
def test_verify_output_rejects_incomplete_tree(context_output, mutation):
    output, files = context_output
    path = output / next(iter(files))
    if mutation == "missing":
        path.unlink()
    elif mutation == "extra":
        (output / "undeclared.md").write_bytes(b"Synthetic extra content")
    else:
        for relative in files:
            (output / relative).write_bytes(b"")
    with pytest.raises((AssertionError, ValueError)):
        verify_output(output, datetime.fromisoformat(FROM))


@pytest.mark.parametrize(
    "changed", [b"Synthetic content", b"Synthetic content\n\n", b"Changed\n"]
)
def test_byte_comparison_rejects_any_content_difference(changed):
    with pytest.raises(AssertionError, match="first differing byte"):
        require_bytes(changed, b"Synthetic content\n", "synthetic.md")


def test_difference_limits_diagnostic_output():
    expected = b"original line\n" * 1000
    actual = (b"changed " + b"x" * 1000 + b"\n") * 1000
    diagnostic = difference(actual, expected, "synthetic.md")
    assert "first differing byte 0" in diagnostic
    lines = diagnostic.splitlines()
    assert len(lines) <= 41 and all(len(line) <= 240 for line in lines)


if __name__ == "__main__":
    if sys.argv[1] == "generate":
        generate(Path(sys.argv[2]))
    else:
        assert sys.argv[1] == "compare" and len(sys.argv) == 4
        compare(Path(sys.argv[2]), Path(sys.argv[3]))
