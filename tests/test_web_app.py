"""Tests for midicrt.clients.web.app -- the aiohttp HTTP/WS wiring around
Bridge. Uses aiohttp's own test utilities (TestServer/TestClient) against a
REAL engine+server fixture on a tmp socket (test_server.py's `make`), same
fixture-reuse discipline as test_web_bridge.py and tests/test_client_base.py.
"""
import asyncio

from aiohttp.test_utils import TestClient, TestServer
from test_server import make

from midicrt.clients import chrome
from midicrt.clients.base import EngineClient
from midicrt.clients.web.app import create_app
from midicrt.clients.web.bridge import Bridge


async def _client_for(tmp_path, allow_control=False, tick_hz=100.0, **cfg):
    """Build a real engine+server fixture, a Bridge pointed at it, and an
    aiohttp TestClient wrapping `create_app`. Returns (eng, srv, task,
    client) -- caller is responsible for `await client.close()` then the
    usual `eng.stop(); await task; await srv.close()` teardown.

    `**cfg` forwards to `make()`/`Config()` (e.g. `pages=[...]`) for tests
    that need to pin the roster explicitly -- see
    `test_describe_endpoint_proxies_engine_describe`'s own comment."""
    eng, srv, task = await make(tmp_path, tick_hz=tick_hz, **cfg)
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
    # Pinned to the 2-page roster this assertion actually cares about
    # (merge-reconciliation fix, docs/phase6-notes.md item 5): this test
    # was written when Config's default roster WAS ["eventlog", "voices"]
    # (pre-phase-4); master's default has since grown to 14 pages
    # (phase-3 task 12's gap ports). Pinning `pages=` here makes that
    # dependency explicit instead of accidentally relying on whatever the
    # global default happens to be today.
    eng, srv, task, client = await _client_for(tmp_path, pages=["eventlog", "voices"])
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


async def test_ws_overlay_status_snapshot_carries_rendered_status_text(tmp_path):
    """End-to-end proof of the merge-reconciliation fix (docs/phase6-notes.md
    item 1): an `overlay.status` snapshot that travels the REAL
    engine->server->bridge->app->websocket path must arrive with a
    `status_text` field equal to `chrome.status_text()` of its own `data`
    -- not just true in bridge.py's own unit tests (test_web_bridge.py),
    but true over the actual wire this browser reads from.

    Drives the check via a `clock_tick` (not just waiting on the
    subscribe-time seeded snapshot): `page.eventlog` and `overlay.status`
    are BOTH seeded at subscribe time and race for the WebSink's maxsize=1
    slot (phase2-notes.md: "never assume ordering"; test_web_bridge.py's/
    test_web_app.py's own helpers work around this same race for THEIR
    topics of interest) -- passively waiting on that initial race was
    observed to flake under full-suite load when `page.eventlog` won it
    and nothing else ever touched `overlay.status` again. A `clock_tick`
    (mirrors tests/test_server.py::
    test_clock_tick_updates_only_overlay_status_over_the_wire) dirties
    ONLY `overlay.status`, never `page.eventlog`, so waiting for
    `beat == 2` -- the clock_tick's specific, unambiguous effect -- can't
    lose this race by construction.
    """
    from midicrt.engine.core import MidiEvent

    eng, srv, task, client = await _client_for(tmp_path, tick_hz=100.0)
    ws = await client.ws_connect("/ws")
    await asyncio.wait_for(ws.receive_json(), timeout=2.0)  # hello

    await eng.queue.put(MidiEvent(0.0, "t", "start", None, None, None, "start"))
    await asyncio.sleep(0.15)
    await eng.queue.put(MidiEvent(0.5, "t", "clock_tick", None, 24, None, "clock_tick",
                                  clock_batch_start=None))

    deadline = asyncio.get_event_loop().time() + 5.0
    status_msg = None
    while asyncio.get_event_loop().time() < deadline and status_msg is None:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
        if (msg.get("kind") == "snapshot" and msg.get("topic") == "overlay.status"
                and msg.get("data", {}).get("beat") == 2):
            status_msg = msg
    assert status_msg is not None, "expected the clock_tick-driven overlay.status snapshot"
    assert status_msg["status_text"] == chrome.status_text(status_msg["data"])

    await ws.close()
    await client.close()
    eng.stop(); await task; await srv.close()


async def test_index_page_has_no_local_status_format_copy(tmp_path):
    """Regression guard for the exact bug docs/phase6-notes.md item 1
    describes: page.html must not keep its own JS copy of the status
    format (that copy silently drifted from chrome.py's real output, with
    no `rec`/REC_MARKER handling at all). The served page should display
    the bridge-rendered `status_text` string verbatim, not recompute
    BAR/BEAT/BPM itself.
    """
    eng, srv, task, client = await _client_for(tmp_path)
    resp = await client.get("/")
    body = await resp.text()
    assert "status_text" in body  # reads the bridge-rendered field...
    assert "BAR ${" not in body   # ...instead of rebuilding the line itself

    await client.close()
    eng.stop(); await task; await srv.close()
