"""Real Summary image smoke script with a loopback-only scripted provider."""

import json
import os

from test_scripted_provider import SELECTED_MODEL, ScriptedProvider, cli, run_provider

CHILD_PROMPT = "Synthetic child task"
ROOT_PROMPT = "Synthetic root task"


class Provider(ScriptedProvider):
    dispatched = False

    def response(self, body):
        text = json.dumps(body.get("messages", []))
        tool = "task" if MAJOR == 1 else "subagent"
        available = {item["function"]["name"] for item in body.get("tools", [])}
        delta = {"content": "Synthetic response"}
        finish = "stop"
        if (
            ROOT_PROMPT in text
            and CHILD_PROMPT not in text
            and tool in available
            and not Provider.dispatched
        ):
            Provider.dispatched = True
            arguments = {
                "description": "Synthetic model inheritance check",
                "prompt": CHILD_PROMPT,
            }
            arguments["subagent_type" if MAJOR == 1 else "agent"] = "general"
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_synthetic",
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(arguments)},
                    }
                ]
            }
            finish = "tool_calls"
        return delta, finish


def run():
    version = cli("--version").strip().removeprefix("opencode v")
    assert version == os.environ["EXPECTED_VERSION"]
    prefix = ["--pure"] if MAJOR == 1 else ["--standalone"]
    help_text = cli(*prefix, "run", "--help")
    assert "model" in help_text and "session" in help_text, help_text
    command = [
        *prefix,
        "run",
        "--format",
        "json",
        "--model",
        f"synthetic/{SELECTED_MODEL}",
        "--auto",
    ]
    with run_provider(Provider):
        initial = cli(*command, ROOT_PROMPT)
        events = [
            json.loads(line) for line in initial.splitlines() if line.startswith("{")
        ]
        session = next(event["sessionID"] for event in events if "sessionID" in event)
        assert Provider.dispatched, initial
        child_requests = [
            body
            for body in Provider.requests
            if CHILD_PROMPT in json.dumps(body.get("messages", []))
            and ROOT_PROMPT not in json.dumps(body.get("messages", []))
        ]
        assert child_requests, (initial, Provider.requests)
        assert all(body["model"] == SELECTED_MODEL for body in child_requests)
        root_requests = [
            body
            for body in Provider.requests
            if ROOT_PROMPT in json.dumps(body.get("messages", []))
        ]
        assert root_requests and all(
            body["model"] == SELECTED_MODEL for body in root_requests
        )
        resumed = cli(*command, "--session", session, "Synthetic recovery task")
        assert session in resumed
        assert any(
            "Synthetic recovery task" in json.dumps(body)
            and body["model"] == SELECTED_MODEL
            for body in Provider.requests
        )
        print(
            f"Summary V{MAJOR} {version}: CLI, Root/Subagent model inheritance, "
            "Recovery state PASS"
        )


if __name__ == "__main__":
    MAJOR = int(os.environ["EXPECTED_VERSION"].split(".")[0])
    run()
