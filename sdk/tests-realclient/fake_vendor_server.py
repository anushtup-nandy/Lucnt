"""One local HTTP server playing both the lucnt backend and the vendor APIs.

Real vendor clients hit it; tests assert on the `/transactions/*` traffic.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class ServerState:
    """Mutable, test-visible record of everything the server saw."""

    authorizes: list[dict[str, Any]] = field(default_factory=list)
    captures: list[dict[str, Any]] = field(default_factory=list)
    voids: list[dict[str, Any]] = field(default_factory=list)
    vendor_calls: list[str] = field(default_factory=list)
    # When True, /transactions/authorize answers `blocked` (budget_exhausted).
    block: bool = False

    def reset(self) -> None:
        self.authorizes.clear()
        self.captures.clear()
        self.voids.clear()
        self.vendor_calls.clear()
        self.block = False


def _chat_completion(model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 1753660800,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "governed hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _chat_sse(model: str) -> bytes:
    chunk = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1753660800,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "hi"},
                "finish_reason": None,
            }
        ],
    }
    done = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1753660800,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    usage = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1753660800,
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return (
        b"".join(
            f"data: {json.dumps(event)}\n\n".encode() for event in (chunk, done, usage)
        )
        + b"data: [DONE]\n\n"
    )


def _responses_payload(model: str) -> dict[str, Any]:
    return {
        "id": "resp-fake",
        "object": "response",
        "created_at": 1753660800,
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "id": "msg-fake",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "governed hello", "annotations": []}
                ],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 36,
            "output_tokens": 87,
            "total_tokens": 123,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _embeddings_payload(model: str) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "model": model,
        "usage": {"prompt_tokens": 8, "total_tokens": 8},
    }


def _anthropic_message(model: str) -> dict[str, Any]:
    return {
        "id": "msg-fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "governed hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 17, "output_tokens": 42},
    }


def _anthropic_sse(model: str) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg-fake",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 17, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "governed hello"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 42},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
        for name, payload in events
    )


class _Handler(BaseHTTPRequestHandler):
    state: ServerState  # set on the server class per instance

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep pytest output readable

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        state = self.state
        path = self.path.split("?", 1)[0]
        body = self._read_body()

        # ---- lucnt backend ------------------------------------------------
        if path == "/transactions/authorize":
            state.authorizes.append(body)
            if state.block:
                self._send_json(
                    {
                        "authorization_id": "00000000-0000-0000-0000-0000000000ff",
                        "status": "blocked",
                        "code": "budget_exhausted",
                        "reason": "monthly budget exhausted (harness)",
                    }
                )
                return
            auth_id = f"00000000-0000-0000-0000-{len(state.authorizes):012d}"
            self._send_json(
                {
                    "authorization_id": auth_id,
                    "status": "approved",
                    "code": "ok",
                    "reason": None,
                }
            )
            return
        if path == "/transactions/capture":
            state.captures.append(body)
            self._send_json(
                {
                    "id": f"tx-{len(state.captures)}",
                    "authorization_id": body.get("authorization_id", ""),
                    "status": "completed",
                    "vendor": "harness",
                    "amount": str(body.get("amount", "1")),
                    "currency": "USD",
                    "estimated": bool(body.get("estimated", False)),
                }
            )
            return
        if path == "/transactions/void":
            state.voids.append(body)
            self._send_json(
                {
                    "authorization_id": body.get("authorization_id", ""),
                    "released_at": "2026-07-28T10:00:00Z",
                    "already_released": False,
                }
            )
            return

        # ---- vendor endpoints ----------------------------------------------
        state.vendor_calls.append(path)
        model = str(body.get("model", "gpt-4o"))
        if path == "/v1/chat/completions":
            if body.get("stream") is True:
                self._send_sse(_chat_sse(model))
            else:
                self._send_json(_chat_completion(model))
            return
        if path == "/v1/responses":
            self._send_json(_responses_payload(model))
            return
        if path == "/v1/embeddings":
            self._send_json(_embeddings_payload(model))
            return
        if path == "/v1/messages/count_tokens":
            self._send_json({"input_tokens": 7})
            return
        if path == "/v1/messages":
            if body.get("stream") is True:
                self._send_sse(_anthropic_sse(model))
            else:
                self._send_json(_anthropic_message(model))
            return
        # Anything else: answer 200 so an ungoverned call would *succeed* —
        # the tests must catch it via the missing ledger rows, not via a 404.
        self._send_json({"ok": True, "path": path})

    def do_GET(self) -> None:  # noqa: N802
        self._send_json({"data": [], "object": "list"})


def vendor_server() -> tuple[ThreadingHTTPServer, ServerState, str]:
    """Start the dual-role server on an ephemeral port.

    Returns:
        `(server, state, base_url)` — call `server.shutdown()` when done.
    """
    state = ServerState()
    handler = type("BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}"
