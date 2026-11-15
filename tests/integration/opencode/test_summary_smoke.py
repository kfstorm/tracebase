"""Real Summary image smoke script with a loopback-only scripted provider."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS = []
CHILD_PROMPT = "Synthetic child task"
ROOT_PROMPT = "Synthetic root task"
SELECTED_MODEL = "selected-model"


class Provider(BaseHTTPRequestHandler):
    dispatched = False

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(body)
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for change, reason in [(delta, None), ({}, finish)]:
            event = {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": 1767225600,
                "model": SELECTED_MODEL,
                "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
            }
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def cli(*arguments):
    result = subprocess.run(
        ["opencode", *arguments],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result.stdout + result.stderr if "--help" in arguments else result.stdout


def run():
    version = cli("--version").strip().removeprefix("opencode v")
    assert version == os.environ["EXPECTED_VERSION"]
    prefix = ["--pure"] if MAJOR == 1 else ["--standalone"]
    help_text = cli(*prefix, "run", "--help")
    assert "model" in help_text and "session" in help_text, help_text
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only the synthetic provider fixture needs config. No model, small_model,
    # agent model, or depth setting can mask --model inheritance in this test.
    os.environ["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {
            "provider": {
                "synthetic": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                        "apiKey": "synthetic-key",
                    },
                    "models": {
                        name: {
                            "name": name,
                            "limit": {"context": 128000, "output": 4096},
                            "tool_call": True,
                        }
                        for name in ("fallback-model", SELECTED_MODEL)
                    },
                }
            },
        }
    )
    os.environ["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
    os.environ["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
    command = [
        *prefix,
        "run",
        "--format",
        "json",
        "--model",
        f"synthetic/{SELECTED_MODEL}",
        "--auto",
    ]
    try:
        initial = cli(*command, ROOT_PROMPT)
        events = [
            json.loads(line) for line in initial.splitlines() if line.startswith("{")
        ]
        session = next(event["sessionID"] for event in events if "sessionID" in event)
        assert Provider.dispatched, initial
        child_requests = [
            body
            for body in REQUESTS
            if CHILD_PROMPT in json.dumps(body.get("messages", []))
            and ROOT_PROMPT not in json.dumps(body.get("messages", []))
        ]
        assert child_requests, (initial, REQUESTS)
        assert all(body["model"] == SELECTED_MODEL for body in child_requests)
        root_requests = [
            body
            for body in REQUESTS
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
            for body in REQUESTS
        )
        print(
            f"Summary V{MAJOR} {version}: CLI, Root/Subagent model inheritance, "
            "Recovery state PASS"
        )
    finally:
        server.shutdown()
        thread.join()


if __name__ == "__main__":
    MAJOR = int(os.environ["EXPECTED_VERSION"].split(".")[0])
    run()
