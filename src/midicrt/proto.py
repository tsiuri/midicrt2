"""Wire protocol: JSON-lines framing and message shapes (spec §3)."""
import json

import midicrt

PROTO_VERSION = "1.0.0"


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


class LineDecoder:
    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        out = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                out.append(json.loads(line))
        return out


def response(id: int, data: dict) -> dict:
    return {"id": id, "ok": True, "data": data}


def error_response(id: int, message: str) -> dict:
    return {"id": id, "ok": False, "error": message}


def snapshot(topic: str, seq: int, data: dict) -> dict:
    return {"kind": "snapshot", "topic": topic, "seq": seq, "data": data}


def event(name: str, data: dict) -> dict:
    return {"kind": "event", "name": name, "data": data}


def hello() -> dict:
    return {"kind": "hello", "proto_version": PROTO_VERSION, "engine_version": midicrt.__version__}


def major(version: str) -> int:
    return int(version.split(".", 1)[0])
