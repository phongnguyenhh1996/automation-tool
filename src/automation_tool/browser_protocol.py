"""
JSON-lines protocol for Browser worker service (request / response / event).

See plan: browser-worker-service (stdio or TCP; each message is one JSON object per line).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

MessageType = Literal["request", "response", "event", "ready"]


def encode_message(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message_line(line: bytes) -> dict[str, Any]:
    s = line.decode("utf-8").strip()
    if not s:
        raise ValueError("empty line")
    return json.loads(s)


@dataclass
class Request:
    request_id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "request", "request_id": self.request_id, "method": self.method, "params": self.params}


@dataclass
class Response:
    request_id: str
    ok: bool
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "response",
            "request_id": self.request_id,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
        }
        return out


@dataclass
class Event:
    event: str
    sub_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "event", "event": self.event, "sub_id": self.sub_id, "payload": self.payload}


# Well-known method names
METHOD_PING = "ping"
METHOD_OPEN_TAB = "open_tab"
METHOD_CLOSE_TAB = "close_tab"
METHOD_GOTO = "goto"
METHOD_QUERY_TEXT = "query_text"
METHOD_EVAL = "eval"
METHOD_SUBSCRIBE_DOM = "subscribe_dom"
METHOD_UNSUBSCRIBE = "unsubscribe"
METHOD_SHUTDOWN = "shutdown"
METHOD_CAPTURE_PIPELINE = "capture_pipeline"
METHOD_CAPTURE_CHARTS = "capture_charts"
METHOD_TV_WATCHLIST_INIT = "tv_watchlist_init"
METHOD_TV_WATCHLIST_POLL = "tv_watchlist_poll"
