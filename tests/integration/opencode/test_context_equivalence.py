"""Generate real conversations in Docker; audit and compare their complete Context."""

import difflib
import itertools
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_scripted_provider import SELECTED_MODEL, ScriptedProvider, cli, run_provider

from tracebase.archive import load_published_archive
from tracebase.context import ContextRequest, generate_context
from tracebase.context_inventory import load_context_inventory
from tracebase.mutable_state import load_mutable_state

DIRECTORY = "/workspace/context-equivalence"
TITLE = "Synthetic context equivalence"
ROOT = "opencode/workspace/context-equivalence/session/01"
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


def difference(actual, expected, path, regions):
    offset = next(
        (
            index
            for index, (a, b) in enumerate(zip(actual, expected, strict=False))
            if a != b
        ),
        min(len(actual), len(expected)),
    )
    lines = actual[:offset].split(b"\n")
    current = actual.split(b"\n")[len(lines) - 1]
    if path.endswith("overview.md") and current.startswith(b"Project directory:"):
        category = "directory"
    elif path.endswith("overview.md") and len(lines) == 1:
        category = "title"
    else:
        category = "body/structure"
    for start, end, label in regions:
        if start <= offset < end:
            category = label
            break
    diff = difflib.unified_diff(
        expected.decode().splitlines(),
        actual.decode().splitlines(),
        fromfile="expected/V1",
        tofile="actual/V2",
        n=2,
        lineterm="",
    )
    limited = "\n".join(line[:240] for line in itertools.islice(diff, 40))
    return f"{path}: first differing byte {offset}; category={category}\n{limited}"


def require_bytes(actual, expected, path, regions=()):
    if actual != expected:
        raise AssertionError(difference(actual, expected, path, regions))


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


def normalize_dialogue(raw, bucket, turns, path):
    """Validate every byte before replacing only known message-header spans."""
    expected = [f"# {bucket.capitalize()}\n\n".encode()]
    normalized = expected.copy()
    regions = []
    offset = len(expected[0])
    for position, (role, timestamp, text) in enumerate(turns, 1):
        prefix = f"**{role.capitalize()} · ".encode()
        suffix = b"**\n\n"
        header = prefix + timestamp.strftime("%Y-%m-%d %H:%M").encode() + suffix
        expected.append(header)
        regions.append((offset, offset + len(prefix), "role/header"))
        regions.append((offset + len(prefix), offset + len(header), "timestamp/header"))
        offset += len(header)
        normalized.append(prefix + f"<TIMESTAMP_{position}>".encode() + suffix)
        body = text.encode() + (b"\n" if position == len(turns) else b"\n\n")
        expected.append(body)
        normalized.append(body)
        regions.append((offset, offset + len(body), "body"))
        offset += len(body)
    require_bytes(raw, b"".join(expected), path, regions)
    return b"".join(normalized)


def verify_output(output, turns, start):
    inventory = load_context_inventory(output)
    state = load_mutable_state(output, inventory)
    assert len(inventory.items) == len(state) == 1
    item = inventory.items[0]
    assert item.root == ROOT and state[0].observations == ()
    request = ContextRequest.parse(*inventory.requested_interval)
    assert request.start == start and request.to_text == TO
    activity = [turn for turn in turns if request.start <= turn[1] < request.end]
    background = [turn for turn in turns if turn[1] < request.start]
    files = {"overview.md", "activity.md"}
    if background:
        files.add("background.md")
    assert set(item.files) == files
    declared = {f"{ROOT}/{file}" for file in files}
    actual = set()
    for path in output.rglob("*"):
        assert not path.is_symlink()
        assert path.resolve().is_relative_to(output.resolve())
        if path.is_file():
            actual.add(path.relative_to(output).as_posix())
    assert actual == declared | {"index.json", "mutable-state.json"}, sorted(actual)
    normalized = {}
    for relative in sorted(declared):
        raw = (output / relative).read_bytes()
        if relative.endswith("overview.md"):
            lines = raw.decode().splitlines()
            assert lines[0] == f"# {TITLE}"
            assert lines.count(f"Project directory: `{DIRECTORY}`") == 1
            assert not any(line.startswith("Working directory:") for line in lines)
            normalized[relative] = raw
        else:
            bucket = Path(relative).stem
            selected = activity if bucket == "activity" else background
            normalized[relative] = normalize_dialogue(raw, bucket, selected, relative)
    return normalized


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
    normalized = {}
    for scenario in SCENARIOS:
        turns = ordinary if scenario == "ordinary" else multi
        start = boundary if scenario == "background" else datetime.fromisoformat(FROM)
        normalized[scenario] = verify_output(results / scenario, turns, start)
    return normalized


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
        generate_context(
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
    generate_context(
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
    print(
        f"Context V1/V2: all {total} Markdown files byte-identical after header-only "
        "timestamp normalization PASS"
    )


@pytest.mark.parametrize(
    "mutation", ["timestamp", "role", "missing", "duplicate", "order", "body"]
)
def test_strict_normalization_rejects_changed_dialogue(mutation):
    turns = [
        ("user", datetime(2026, 1, 1, tzinfo=UTC), "Question"),
        ("assistant", datetime(2026, 1, 1, tzinfo=UTC), "Answer"),
    ]
    original = (
        b"# Activity\n\n**User \xc2\xb7 2026-01-01 00:00**\n\nQuestion\n\n"
        b"**Assistant \xc2\xb7 2026-01-01 00:00**\n\nAnswer\n"
    )
    mutations = {
        "timestamp": original.replace(b"00:00", b"00:01", 1),
        "role": original.replace(b"User", b"Assistant", 1),
        "missing": original[: original.index(b"**Assistant")],
        "duplicate": original + original,
        "order": original.replace(b"Question", b"PLACEHOLDER")
        .replace(b"Answer", b"Question")
        .replace(b"PLACEHOLDER", b"Answer"),
        "body": original.replace(b"Question", b"Changed"),
    }
    with pytest.raises(AssertionError, match="first differing byte"):
        normalize_dialogue(mutations[mutation], "activity", turns, "activity.md")


def test_strict_normalization_preserves_heading_like_body_timestamp():
    time = datetime(2026, 1, 1, tzinfo=UTC)
    text = ANSWERS[0]
    raw = f"# Activity\n\n**Assistant · 2026-01-01 00:00**\n\n{text}\n".encode()
    normalized = normalize_dialogue(
        raw, "activity", [("assistant", time, text)], "activity.md"
    )
    assert (
        normalized
        == f"# Activity\n\n**Assistant · <TIMESTAMP_1>**\n\n{text}\n".encode()
    )
    changed_body = raw.replace(b"12:34", b"12:35")
    with pytest.raises(AssertionError, match="category=body"):
        normalize_dialogue(
            changed_body, "activity", [("assistant", time, text)], "activity.md"
        )


def test_difference_limits_diagnostic_output():
    expected = b"original line\n" * 1000
    actual = (b"changed " + b"x" * 1000 + b"\n") * 1000
    diagnostic = difference(actual, expected, "activity.md", ())
    assert "first differing byte 0" in diagnostic
    lines = diagnostic.splitlines()
    assert len(lines) <= 41 and all(len(line) <= 240 for line in lines)


if __name__ == "__main__":
    if sys.argv[1] == "generate":
        generate(Path(sys.argv[2]))
    else:
        assert sys.argv[1] == "compare" and len(sys.argv) == 4
        compare(Path(sys.argv[2]), Path(sys.argv[3]))
