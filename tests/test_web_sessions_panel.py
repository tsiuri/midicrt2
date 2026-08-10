"""Web-layer proof for the Phase 9 Task 6 sessions panel: `capture.
sessions_list`/`capture.sessions_show` reachable through the SAME `/api/
action` endpoint (+ `ALLOW_CONTROL` gate) every other panel here already
uses, and the served page carries the panel's own markup. Mirrors test_
web_app.py's own `_client_for`/TestClient fixture-reuse discipline -- the
engine-level action WIRING (registration, ActionError translation,
live-session labeling) is covered directly in test_engine_sessions_
actions.py instead.
"""
from aiohttp.test_utils import TestClient, TestServer
from test_server import make

from midicrt.clients.base import EngineClient
from midicrt.clients.web.app import create_app
from midicrt.clients.web.bridge import Bridge


async def _client_for(tmp_path, allow_control=True):
    eng, srv, task = await make(tmp_path)
    engine_client = EngineClient(srv.socket_path)
    bridge = Bridge(engine_client, subscribe_rate=50.0)
    app = create_app(bridge, allow_control=allow_control)
    client = TestClient(TestServer(app))
    await client.start_server()
    return eng, srv, task, client


async def test_sessions_list_action_reachable_via_api_action_with_control_on(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.post("/api/action", json={"name": "capture.sessions_list", "args": {}})
    assert resp.status == 200
    data = await resp.json()
    assert data["sessions"] == []

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_sessions_list_action_403_without_allow_control(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=False)
    resp = await client.post("/api/action", json={"name": "capture.sessions_list", "args": {}})
    assert resp.status == 403

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_sessions_list_reports_a_session_started_and_stopped_through_the_api(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    start_resp = await client.post("/api/action", json={"name": "capture.start", "args": {}})
    start_data = await start_resp.json()
    await client.post("/api/action", json={"name": "capture.stop", "args": {}})

    resp = await client.post("/api/action", json={"name": "capture.sessions_list", "args": {}})
    data = await resp.json()
    ids = [row["id"] for row in data["sessions"]]
    assert start_data["session_id"] in ids

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_sessions_show_action_reachable_via_api_action(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    start_resp = await client.post("/api/action", json={"name": "capture.start", "args": {}})
    start_data = await start_resp.json()
    await client.post("/api/action", json={"name": "capture.stop", "args": {}})

    resp = await client.post(
        "/api/action", json={"name": "capture.sessions_show", "args": {"id": start_data["session_id"]}})
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == start_data["session_id"]
    assert data["replay"] is not None

    await client.close()
    eng.stop(); await task; await srv.close()


async def test_sessions_show_action_400_for_unknown_id(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.post(
        "/api/action", json={"name": "capture.sessions_show", "args": {"id": "nope"}})
    assert resp.status == 400

    await client.close()
    eng.stop(); await task; await srv.close()


# -- served page carries the panel's own markup ------------------------------

async def test_index_page_carries_the_sessions_panel_markup(tmp_path):
    eng, srv, task, client = await _client_for(tmp_path, allow_control=True)
    resp = await client.get("/")
    body = await resp.text()
    assert 'id="sessions-panel"' in body
    assert 'id="sessions-list"' in body
    assert "capture.sessions_list" in body
    assert "capture.sessions_show" in body
    # v1 scope: no trim/delete affordance from the browser (task brief).
    assert "sessions.trim" not in body
    assert "sessions_trim" not in body
    assert "sessions.delete" not in body

    await client.close()
    eng.stop(); await task; await srv.close()
