"""Loopback-only OpenAI-compatible provider shared by real CLI tests."""

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

SELECTED_MODEL = "selected-model"


class ScriptedProvider(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []

    def log_message(self, *_args):
        pass

    def response(self, body):
        raise NotImplementedError

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        delta, finish = self.response(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for change, reason in [(delta, None), ({}, finish)]:
            event = {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": 1767225600,
                "model": body["model"],
                "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
            }
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def cli(*arguments, cwd=None, input_text=None):
    result = subprocess.run(
        ["opencode", *arguments],
        cwd=cwd,
        # OpenCode resolves project location through PWD as well as getcwd().
        env=os.environ | ({"PWD": str(cwd)} if cwd is not None else {}),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-2000:])
    return result.stdout + result.stderr if "--help" in arguments else result.stdout


@contextmanager
def run_provider(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Provider/model inventory only: defaults must not mask CLI model selection.
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
            }
        }
    )
    os.environ["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
    os.environ["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
    try:
        yield
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
