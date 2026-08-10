"""TDD for `Engine`'s wiring of the two READ-ONLY `capture.sessions_list`/
`capture.sessions_show` actions (Phase 9 Task 6, capture editor) -- action
registration/schema, delegation to engine/sessions.py's pure functions
against `self._capture.dir`, `ActionError` translation, and (the one thing
that genuinely needs a real `Engine`, not a bare `engine/sessions.py` call)
live-session labeling straight off `self._capture.is_recording`/`.status()`
with NO socket round-trip. `engine/sessions.py`'s OWN contract (drift
handling, trim windowing, stage-don't-delete) is covered directly in
test_sessions.py instead -- mirrors test_engine_sysex_store.py's own "pure
logic here, registry-aware engine wiring there" split.
"""
import os

import pytest

from midicrt.config import Config
from midicrt.engine.actions import ActionError
from midicrt.engine.core import Engine

# -- action registration ------------------------------------------------------

def test_sessions_actions_are_registered_with_expected_schema():
    eng = Engine(Config())
    desc = eng.actions.describe()
    assert desc["capture.sessions_list"]["args"] == {}
    assert desc["capture.sessions_show"]["args"] == {"id": "str"}


# -- capture.sessions_list -----------------------------------------------------

async def test_sessions_list_action_is_empty_for_a_fresh_engine():
    eng = Engine(Config())
    result = await eng.actions.dispatch("capture.sessions_list", {})
    assert result == {"capture_dir": eng._capture.dir, "sessions": []}


async def test_sessions_list_action_reports_a_stopped_session():
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    await eng.actions.dispatch("capture.stop", {})
    result = await eng.actions.dispatch("capture.sessions_list", {})
    ids = [row["id"] for row in result["sessions"]]
    assert start["session_id"] in ids
    row = next(r for r in result["sessions"] if r["id"] == start["session_id"])
    assert row["status"] == "finished"


async def test_sessions_list_action_labels_the_live_session_as_recording():
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    result = await eng.actions.dispatch("capture.sessions_list", {})
    row = next(r for r in result["sessions"] if r["id"] == start["session_id"])
    assert row["status"] == "recording"
    await eng.actions.dispatch("capture.stop", {})


# -- capture.sessions_show -----------------------------------------------------

async def test_sessions_show_action_unknown_id_raises_action_error():
    eng = Engine(Config())
    with pytest.raises(ActionError, match="unknown session"):
        await eng.actions.dispatch("capture.sessions_show", {"id": "no-such-session"})


async def test_sessions_show_action_summarizes_a_stopped_session():
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    await eng.actions.dispatch("capture.stop", {})
    result = await eng.actions.dispatch("capture.sessions_show", {"id": start["session_id"]})
    assert result["id"] == start["session_id"]
    assert result["status"] == "finished"
    assert result["replay"] is not None
    assert "action" in result["replay"]["marks_by_kind"]   # capture.start/.stop marks


async def test_sessions_show_action_labels_the_live_session_as_recording():
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    result = await eng.actions.dispatch("capture.sessions_show", {"id": start["session_id"]})
    assert result["status"] == "recording"
    assert result["ended_ts"] is None
    await eng.actions.dispatch("capture.stop", {})


# -- read-only: neither action ever mutates index.json -----------------------

async def test_sessions_list_and_show_never_touch_index_json_mtime():
    eng = Engine(Config())
    start = await eng.actions.dispatch("capture.start", {})
    await eng.actions.dispatch("capture.stop", {})
    index_path = os.path.join(eng._capture.dir, "index.json")
    before = os.path.getmtime(index_path)
    await eng.actions.dispatch("capture.sessions_list", {})
    await eng.actions.dispatch("capture.sessions_show", {"id": start["session_id"]})
    assert os.path.getmtime(index_path) == before
