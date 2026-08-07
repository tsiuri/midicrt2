"""Tests for midicrt.clients.web.app -- the aiohttp HTTP/WS wiring around
Bridge. Uses aiohttp's own test utilities (TestServer/TestClient) against a
REAL engine+server fixture on a tmp socket (test_server.py's `make`), same
fixture-reuse discipline as test_web_bridge.py and tests/test_client_base.py.
"""
import asyncio

from aiohttp.test_utils import TestClient, TestServer
from test_server import make

from midicrt.clients.base import EngineClient
from midicrt.clients.web.app import create_app
from midicrt.clients.web.bridge import Bridge


async def _client_for(tmp_path, allow_control=False, tick_hz=100.0):
    """Build a real engine+server fixture, a Bridge pointed at it, and an
    aiohttp TestClient wrapping `create_app`. Returns (eng, srv, task,
    client) -- caller is responsible for `await client.close()` then the
    usual `eng.stop(); await task; await srv.close()` teardown."""
    eng, srv, task = await make(tmp_path, tick_hz=tick_hz)
    engine_client = EngineClient(srv.socket_path)
    bridge = Bridge(engine_client, subscribe_rate=50.0)
    app = create_app(bridge, allow_control=allow_control)
    client = TestClient(TestServer(app))
    await client.start_server()
    return eng, srv, task, client


async def test_index_serves_html_and_reflects_allow_control_flag(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=False)
    resp = await client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.content_type
    body = await resp.text()
    assert "ALLOW_CONTROL = false" in body

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_index_reflects_allow_control_true(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.get("/")
    body = await resp.text()
    assert "ALLOW_CONTROL = true" in body

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_describe_endpoint_proxies_engine_describe(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path)
    resp = await client.get("/api/describe")
    assert resp.status == 200
    data = await resp.json()
    assert data["pages"] == ["eventlog", "voices"]
    assert "eventlog.clear" in data["actions"]
    assert data["current_page"] == "eventlog"

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_action_endpoint_403_without_allow_control(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=False)
    resp = await client.post("/api/action", json={"name": "page.next", "args": {}})
    assert resp.status == 403

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_action_endpoint_works_with_allow_control(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.post("/api/action", json={"name": "page.next", "args": {}})
    assert resp.status == 200
    data = await resp.json()
    assert data["page"] == "voices"

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_action_endpoint_rejects_missing_name(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.post("/api/action", json={"args": {}})
    assert resp.status == 400

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_action_endpoint_surfaces_engine_rejection_as_400(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.post("/api/action", json={"name": "bogus-action", "args": {}})
    assert resp.status == 400

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_ws_delivers_hello_then_a_snapshot_end_to_end(tmp_path):
    from midicrt.engine.core import MidiEvent

    eng, srv, task, client = await _client_for(tmp_path, tick_hz=100.0)
    ws = await client.ws_connect("/ws")

    hello = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
    assert hello["kind"] == "hello"
    assert hello["page"] == "eventlog"
    assert hello["topic"] == "page.eventlog"

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "n0"))

    seen = {}
    deadline = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline and "page.eventlog" not in seen:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=3.0)
        if msg.get("kind") == "snapshot":
            seen[msg["topic"]] = msg
    assert "page.eventlog" in seen
    assert seen["page.eventlog"]["data"]["lines"][-1]["text"] == "n0"

    await ws.close()
    await client.close()
    eng.stop(); await task; await srv.close()


async def test_ws_multiple_browsers_share_one_engine_connection(tmp_path):
    from midicrt.engine.core import MidiEvent

    eng, srv, task, client = await _client_for(tmp_path, tick_hz=100.0)
    ws1 = await client.ws_connect("/ws")
    ws2 = await client.ws_connect("/ws")
    await asyncio.wait_for(ws1.receive_json(), timeout=2.0)  # hello
    await asyncio.wait_for(ws2.receive_json(), timeout=2.0)  # hello

    # Only ONE EngineClient connection should exist server-side despite two
    # open websockets -- proves the fan-out, not two independent bridges.
    assert srv.clients == 1

    await eng.queue.put(MidiEvent(0, "t", "note_on", 0, 60, 1, "shared"))

    async def _wait_for_eventlog(ws):
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3.0)
            if msg.get("kind") == "snapshot" and msg.get("topic") == "page.eventlog":
                return msg
        raise AssertionError("no page.eventlog snapshot seen")

    m1 = await _wait_for_eventlog(ws1)
    m2 = await _wait_for_eventlog(ws2)
    assert m1["data"]["lines"][-1]["text"] == "shared"
    assert m2["data"]["lines"][-1]["text"] == "shared"

    await ws1.close(); await ws2.close()
    await client.close()
    eng.stop(); await task; await srv.close()
