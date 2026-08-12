import queue
import threading
import time
from itertools import pairwise

from midicrt.clients import tui
from midicrt.clients.base import ClientError
from midicrt.clients.chrome import (
    DEFAULT_ALERTS_VM,
    DEFAULT_BEATFLASH_VM,
    DEFAULT_LOOPPROGRESS_VM,
    DEFAULT_STATUS_VM,
    DEFAULT_TIMESIG_VM,
    beatprogress_row_text,
    secondary_status_text,
    status_text,
)
from midicrt.clients.tui import (
    RENDERERS,
    _config_body_lines,
    _help_body_lines,
    _img2txtviz_grid_lines,
    _pianoroll_grid,
    _render_unknown,
    _roll_glyph,
    _spectrum_bar_rows,
    _spectrum_columns,
    _voices_bar,
    render_beatprogress_row,
    render_config_lines,
    render_harmony_lines,
    render_help_lines,
    render_help_overlay_box,
    render_img2txtviz_lines,
    render_lines,
    render_pianoroll_lines,
    render_screensaver_lines,
    render_secondary_row,
    render_spectrum_lines,
    render_status_row,
    render_tuner_lines,
    render_voices_lines,
    run_tui,
    screensaver_row_texts,
)

VM = {"title": "EVENT LOG", "count": 12,
      "lines": [{"text": f"line{i}", "style": "normal"} for i in range(10)]}


def test_render_geometry_and_content():
    out = render_lines(VM, width=30, height=5)
    assert len(out) == 5
    assert all(len(line) == 30 for line in out)
    assert out[0].startswith("EVENT LOG  (12 events)")
    assert out[-1].strip() == "line9"          # newest last
    assert out[1].strip() == "line6"           # only the tail fits


def test_render_truncates_long_lines():
    vm = {"title": "EVENT LOG", "count": 1,
          "lines": [{"text": "x" * 100, "style": "normal"}]}
    out = render_lines(vm, width=10, height=2)
    assert all(len(line) == 10 for line in out)


def test_render_empty():
    out = render_lines({"title": "EVENT LOG", "count": 0, "lines": []}, 20, 3)
    assert len(out) == 3 and out[2] == " " * 20


def test_renderers_dispatch_table_has_eventlog():
    assert RENDERERS["eventlog"] is render_lines


def test_key_actions_are_now_keymap_driven_not_a_hardcoded_module_dict():
    # Phase 4 Task 1 (docs/phase4-notes.md): the old module-level
    # `_KEY_ACTIONS` dict is gone entirely -- `run_tui` now builds its key
    # dispatch from `fetch_keymap(client)` at connect (see
    # `test_run_tui_dispatches_a_remapped_key_from_the_fetched_keymap`
    # below for the resolution path itself, and test_client_base.py's
    # `dispatch_key`/`fetch_keymap` tests for the shared machinery).
    assert not hasattr(tui, "_KEY_ACTIONS")


# -- action-dispatch failure vs. connection loss (bindings review,           -
# live-reproduced Critical finding) -------------------------------------
#
# `dispatch_key` (clients/base.py) raises `ClientError` for BOTH a
# genuinely dead connection AND a rejected action (bad/missing args,
# unknown action). The OLD `run_tui` key-handling treated any such
# `ClientError` as "connection lost" and exited the whole client -- latent
# since the old hardcoded `_KEY_ACTIONS` only ever held zero-arg actions,
# newly REACHABLE now that a keymap.toml can name literally any action.
# `_handle_key_press` isolates the fix: it must ALWAYS absorb a
# `ClientError` (recording it as a transient status message) and never
# propagate/exit, no matter what the exception says -- a genuine
# disconnect is detected authoritatively elsewhere (the reader thread's
# own EOF/None sentinel via `drain_latest`'s/`wait_first_snapshot`'s OWN
# `ClientError`), never by interpreting THIS one's text.

class _RejectingClient:
    """Fake client whose `action()` always raises ClientError -- the exact
    shape a real rejected action (missing/bad args, unknown action) takes
    at this layer, indistinguishable BY TYPE from a lost connection."""

    def __init__(self, message="missing arg: name"):
        self.message = message
        self.calls: list[str] = []

    def action(self, name, args=None):
        self.calls.append(name)
        raise ClientError(self.message)


class _RecordingClient:
    def __init__(self):
        self.calls: list[str] = []

    def action(self, name, args=None):
        self.calls.append(name)


# -- _normalize_key (Phase 10 Task A, docs/demo-feedback-2026-08-12.md
# item 11) -------------------------------------------------------------

class _FakeKeystroke:
    """Trivial duck-typed stand-in for a `blessed.Keystroke` -- no real
    Terminal/tty needed (see `_normalize_key`'s own docstring for why it
    was extracted specifically to make this possible)."""

    def __init__(self, s: str, is_sequence: bool = False, name: str | None = None):
        self._s = s
        self.is_sequence = is_sequence
        self.name = name

    def __str__(self) -> str:
        return self._s


def test_normalize_key_passes_through_an_ordinary_printable_char():
    assert tui._normalize_key(_FakeKeystroke("d")) == "d"


def test_normalize_key_passes_through_blank_on_a_timeout_with_no_keypress():
    assert tui._normalize_key(_FakeKeystroke("")) == ""


def test_normalize_key_resolves_a_sequence_key_to_its_symbolic_name():
    # The actual fix: str(key) on a sequence would be the raw escape code
    # (e.g. "\x1b[A" for up-arrow on most terminals) -- never matches
    # anything in a keymap keyed by friendly names like "KEY_UP".
    key = _FakeKeystroke("\x1b[A", is_sequence=True, name="KEY_UP")
    assert tui._normalize_key(key) == "KEY_UP"
    key = _FakeKeystroke("\x1b[B", is_sequence=True, name="KEY_DOWN")
    assert tui._normalize_key(key) == "KEY_DOWN"


def test_normalize_key_unnamed_sequence_becomes_blank_not_none():
    # Defensive: a sequence blessed recognizes as "is a sequence" but has
    # no symbolic name for (name=None) must still return a str (never
    # None) -- dispatch_key's keymap.get(key) is safe either way, but this
    # keeps _normalize_key's own return type honestly `-> str` always.
    assert tui._normalize_key(_FakeKeystroke("\x1bOP", is_sequence=True, name=None)) == ""


def test_handle_key_press_returns_true_only_for_client_quit():
    state = {"keymap": {"q": "client.quit"}}
    assert tui._handle_key_press(_RecordingClient(), "q", state) is True


def test_handle_key_press_absorbs_a_rejected_action_without_exiting():
    # THE fix: a `page.goto`-shaped binding (needs an arg dispatch_key can
    # never supply) getting rejected by the engine must not propagate.
    state = {"keymap": {"g": "page.goto"}}
    client = _RejectingClient("missing arg: name")
    result = tui._handle_key_press(client, "g", state)
    assert result is False   # never signals "quit" for an action failure
    assert client.calls == ["page.goto"]
    assert "missing arg" in state["last_error"]


def test_handle_key_press_unmapped_key_does_not_touch_last_error():
    state = {"keymap": {"q": "client.quit"}}
    client = _RecordingClient()
    assert tui._handle_key_press(client, "z", state) is False
    assert client.calls == []
    assert state.get("last_error") is None


# -- Phase 8 Task 6: help overlay toggle/swallow (docs/gui-phase-decisions- --
# -- 2026-08-08.md keymap revamp) --------------------------------------------

def test_handle_key_press_help_toggle_arms_overlay_without_calling_action():
    state = {"keymap": {"?": "client.help_toggle"}}
    client = _RecordingClient()
    result = tui._handle_key_press(client, "?", state)
    assert result is False
    assert state["help_overlay"] is True
    assert client.calls == []


def test_handle_key_press_swallows_any_key_while_overlay_active():
    state = {"keymap": {"c": "eventlog.clear"}, "help_overlay": True}
    client = _RecordingClient()
    result = tui._handle_key_press(client, "c", state)
    assert result is False
    assert state["help_overlay"] is False   # dismissed
    assert client.calls == []               # never reached the engine


def test_handle_key_press_quit_still_works_when_overlay_is_not_active():
    state = {"keymap": {"q": "client.quit"}, "help_overlay": False}
    assert tui._handle_key_press(_RecordingClient(), "q", state) is True


# -- Phase 8 Task 6: TUI's boxed help-overlay panel --------------------------

def test_render_help_overlay_box_global_and_page_sections():
    box_x, box_y, rows = render_help_overlay_box(
        {"q": "client.quit"}, {"p": "pianoroll.projection_toggle"}, "pianoroll",
        width=60, height=20)
    assert rows[0].startswith("+") and rows[0].endswith("+")
    assert rows[-1] == rows[0]   # top/bottom borders match
    assert any("GLOBAL" in row for row in rows)
    assert any("PIANOROLL" in row for row in rows)
    assert any("client.quit" in row for row in rows)
    assert 0 <= box_x < 60 and 0 <= box_y < 20


def test_render_help_overlay_box_centers_within_the_given_dimensions():
    box_x, box_y, rows = render_help_overlay_box({"q": "client.quit"}, {}, "eventlog",
                                                  width=80, height=24)
    box_w = len(rows[0])
    assert box_x == (80 - box_w) // 2
    assert box_y == (24 - len(rows)) // 2


def test_render_help_overlay_box_falls_back_to_placeholder_when_empty():
    _box_x, _box_y, rows = render_help_overlay_box({}, {}, "eventlog", width=40, height=10)
    assert any("no bindings" in row for row in rows)


def test_render_help_overlay_box_every_row_is_the_same_width():
    _box_x, _box_y, rows = render_help_overlay_box(
        {"q": "client.quit", "n": "page.next"}, {}, "eventlog", width=60, height=20)
    widths = {len(row) for row in rows}
    assert len(widths) == 1


def test_render_help_overlay_box_resolves_page_jump_with_a_roster():
    _box_x, _box_y, rows = render_help_overlay_box(
        {"2": {"action": "page.jump", "args": {"position": 2}}}, {}, "eventlog",
        width=60, height=20, roster=["eventlog", "voices"])
    assert any("-> voices" in row for row in rows)


def test_active_error_text_none_when_no_error_recorded():
    assert tui._active_error_text({}) is None


def test_active_error_text_visible_within_display_window():
    state = {"last_error": "boom", "last_error_until": time.time() + 10}
    assert tui._active_error_text(state) == "boom"


def test_active_error_text_expires_after_display_window():
    state = {"last_error": "boom", "last_error_until": time.time() - 1}
    assert tui._active_error_text(state) is None


# -- DAW-style MIDI learn status-line flash (Phase 4 Task 3,
# docs/phase4-notes.md) ------------------------------------------------------
#
# `learn_bound`/`learn_cancelled` reuse T1's transient-message MECHANISM
# (same fixed display window, same "overwrite the status line while active"
# shape as `_handle_key_press`'s own `last_error`/`_active_error_text` right
# above) via a PARALLEL state slot (`learn_msg`/`learn_msg_until`) rather
# than the literal same one -- a learn outcome is not an error (bound
# successfully is good news), and the two must be able to coexist without
# one silently clobbering the other's expiry. `learn_armed` itself is NOT
# transient -- it's a STICKY flag that stays true for as long as the arm is
# outstanding (there's no fixed duration for "how long a human takes to hit
# a key/knob"), cleared the moment either outcome event arrives.

def test_set_learn_message_and_active_learn_message_round_trip():
    state = {}
    tui._set_learn_message(state, "LEARN: bound to b1")
    assert tui._active_learn_message(state) == "LEARN: bound to b1"


def test_active_learn_message_none_when_nothing_recorded():
    assert tui._active_learn_message({}) is None


def test_active_learn_message_expires_after_display_window():
    state = {"learn_msg": "LEARN: bound to b1", "learn_msg_until": time.time() - 1}
    assert tui._active_learn_message(state) is None


def test_apply_learn_event_armed_sets_the_sticky_flag():
    state = {"learn_armed": False}
    tui._apply_learn_event(state, {"kind": "event", "name": "learn_armed",
                                   "data": {"action": "page.next", "mode": "trigger"}})
    assert state["learn_armed"] is True
    assert tui._active_learn_message(state) is None   # armed alone is not a transient flash


def test_apply_learn_event_bound_clears_the_sticky_flag_and_sets_a_transient_message():
    state = {"learn_armed": True}
    tui._apply_learn_event(state, {"kind": "event", "name": "learn_bound",
                                   "data": {"binding": {"id": "learn_123", "action": "page.next"}}})
    assert state["learn_armed"] is False
    assert "learn_123" in tui._active_learn_message(state)


def test_apply_learn_event_cancelled_clears_the_sticky_flag_and_sets_a_transient_message():
    state = {"learn_armed": True}
    tui._apply_learn_event(state, {"kind": "event", "name": "learn_cancelled",
                                   "data": {"reason": "timeout"}})
    assert state["learn_armed"] is False
    assert "timeout" in tui._active_learn_message(state)


def test_run_tui_handles_learn_events_without_crashing(monkeypatch):
    """`learn_armed`/`learn_bound`/`learn_cancelled` arriving mid-session
    must never crash `run_tui`'s main loop -- proven the same
    scripted-inbox/FakeEngineClient/background-thread technique as
    test_run_tui_refetches_keymap_on_keymap_changed_event above; the
    state-mutation logic itself is unit-tested directly (the four tests
    right above), so this only needs to prove the `on_event` WIRING
    doesn't crash and the loop still shuts down cleanly afterward."""
    inbox = queue.Queue()
    inbox.put({"kind": "snapshot", "topic": "page.eventlog",
               "data": {"title": "EVENT LOG", "count": 0, "lines": []}})
    inbox.put({"kind": "event", "name": "learn_armed",
               "data": {"action": "page.next", "mode": "trigger", "args": {}}})
    inbox.put({"kind": "event", "name": "learn_bound",
               "data": {"binding": {"id": "learn_1", "action": "page.next"}}})

    class FakeEngineClient:
        def __init__(self, socket_path):
            pass

        def connect(self):
            pass

        def request(self, cmd):
            return {"data": {"current_page": "eventlog", "keymap": {}}}

        def subscribe(self, topics, max_rate):
            pass

        def unsubscribe(self, topics):
            pass

        def start_reader(self):
            return inbox

        def action(self, name, args=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tui, "EngineClient", FakeEngineClient)

    outcome = {}

    def target():
        try:
            outcome["result"] = run_tui("/tmp/unused.sock")
        except BaseException as exc:  # noqa: BLE001 -- capture ANY crash
            outcome["exception"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert "exception" not in outcome, (
        f"run_tui crashed handling learn events: {outcome.get('exception')!r}")

    inbox.put(None)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "run_tui did not exit after the shutdown sentinel"


def _make_fake_engine_client(inbox, describe_extra=None):
    """Shared fake for the two show_fps tests below -- same shape as
    `test_run_tui_handles_learn_events_without_crashing`'s own inline
    `FakeEngineClient`, factored out only because both new tests need it
    with a different `describe()` payload (`show_fps` True vs absent)."""
    describe_data = {"current_page": "eventlog", "keymap": {}}
    if describe_extra:
        describe_data.update(describe_extra)

    class FakeEngineClient:
        def __init__(self, socket_path):
            pass

        def connect(self):
            pass

        def request(self, cmd):
            return {"data": dict(describe_data)}

        def subscribe(self, topics, max_rate):
            pass

        def unsubscribe(self, topics):
            pass

        def start_reader(self):
            return inbox

        def action(self, name, args=None):
            pass

        def close(self):
            pass

    return FakeEngineClient


def test_run_tui_shows_fps_readout_in_status_row_when_show_fps_configured(monkeypatch, capsys):
    """Phase 10 Task A (docs/demo-feedback-2026-08-12.md item 4): with
    `config.show_fps` True (surfaced via `describe`'s new field, see
    clients/base.py::fetch_show_fps), the printed status row carries a
    `"fps:"` readout -- proven end-to-end (not just the pure `chrome.
    format_fps`/`header_with_hint` unit tests) via the same scripted-
    inbox/FakeEngineClient/background-thread technique the learn-events
    test above uses, capturing real stdout since `run_tui` writes frames
    with a plain `print(..., flush=True)` (blessed degrades gracefully
    off a real tty, no monkeypatch needed for that part)."""
    inbox = queue.Queue()
    inbox.put({"kind": "snapshot", "topic": "page.eventlog",
               "data": {"title": "EVENT LOG", "count": 0, "lines": []}})
    monkeypatch.setattr(tui, "EngineClient",
                        _make_fake_engine_client(inbox, {"show_fps": True}))

    outcome = {}

    def target():
        try:
            outcome["result"] = run_tui("/tmp/unused.sock")
        except BaseException as exc:  # noqa: BLE001 -- capture ANY crash
            outcome["exception"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=1.0)
    inbox.put(None)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "run_tui did not exit after the shutdown sentinel"
    assert "exception" not in outcome, f"run_tui crashed: {outcome.get('exception')!r}"
    captured = capsys.readouterr()
    assert "fps:" in captured.out


def test_run_tui_omits_fps_readout_when_show_fps_not_configured(monkeypatch, capsys):
    """Mirror of the test above with the config gate OFF (the default --
    an older server predating the field, or a fresh describe() with no
    override, both fall back to False per fetch_show_fps's own defensive
    contract) -- the status row must never carry "fps:" text."""
    inbox = queue.Queue()
    inbox.put({"kind": "snapshot", "topic": "page.eventlog",
               "data": {"title": "EVENT LOG", "count": 0, "lines": []}})
    monkeypatch.setattr(tui, "EngineClient", _make_fake_engine_client(inbox))

    outcome = {}

    def target():
        try:
            outcome["result"] = run_tui("/tmp/unused.sock")
        except BaseException as exc:  # noqa: BLE001 -- capture ANY crash
            outcome["exception"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=1.0)
    inbox.put(None)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "run_tui did not exit after the shutdown sentinel"
    assert "exception" not in outcome, f"run_tui crashed: {outcome.get('exception')!r}"
    captured = capsys.readouterr()
    assert "fps:" not in captured.out


def test_run_tui_repaints_pianoroll_every_tick_with_extrapolated_positions(monkeypatch):
    """Phase 10 Task A (docs/demo-feedback-2026-08-12.md items 3+9): TUI's
    own end-to-end proof, mirroring test_fb_render.py::test_run_device_
    repaints_pianoroll_every_tick_with_extrapolated_positions -- with only
    ONE real page.pianoroll snapshot ever delivered, `run_tui`'s own idle
    loop ticks (`term.inkey(timeout=0.05)` pacing, no new content) must
    still call the pianoroll renderer MULTIPLE times with a note x0 that
    keeps moving (`chrome.extrapolate_pianoroll_vm`), not the same static
    vm redrawn."""
    seen_x0s = []
    real_render_pianoroll_lines = render_pianoroll_lines

    def spy_render_pianoroll_lines(vm, width, height):
        seen_x0s.append(vm["notes"][0]["x0"])
        return real_render_pianoroll_lines(vm, width, height)

    monkeypatch.setitem(RENDERERS, "pianoroll", spy_render_pianoroll_lines)

    inbox = queue.Queue()
    now = time.time()
    inbox.put({
        "kind": "snapshot", "topic": "page.pianoroll",
        "data": {
            "title": "PIANOROLL",
            "notes": [{"ch": 1, "y": 0.5, "x0": 0.9, "x1": 1.0, "vel": 0.8, "active": False}],
            "window": {"mode": "tempo", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0,
                      "origin_ts": now, "velocity": -2.0, "running": True},
            "range": {"lo": 36, "hi": 83},
            "grid": {"beat_xs": [], "bar_xs": [], "pitch_guide_ys": [], "running": True},
            "row_tint": [], "overlap_flash": [],
        },
    })

    monkeypatch.setattr(
        tui, "EngineClient",
        _make_fake_engine_client(inbox, {"current_page": "pianoroll"}))

    outcome = {}

    def target():
        try:
            outcome["result"] = run_tui("/tmp/unused.sock")
        except BaseException as exc:  # noqa: BLE001 -- capture ANY crash
            outcome["exception"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=0.6)
    inbox.put(None)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "run_tui did not exit after the shutdown sentinel"
    assert "exception" not in outcome, f"run_tui crashed: {outcome.get('exception')!r}"

    assert len(seen_x0s) >= 3, (
        f"expected several forced pianoroll repaints from one snapshot, got {seen_x0s!r}")
    assert all(a >= b for a, b in pairwise(seen_x0s)), (
        f"expected non-increasing x0 as the paper scrolls, got {seen_x0s!r}")
    assert seen_x0s[-1] < seen_x0s[0]


def test_render_unknown_fallback_has_no_crash_on_bare_vm():
    out = _render_unknown({}, width=20, height=3)
    assert len(out) == 3
    assert all(len(line) == 20 for line in out)


# -- chrome: status row (phase-3 task 3) -------------------------------------

def test_render_status_row_is_exactly_width_wide():
    row = render_status_row(DEFAULT_STATUS_VM, width=15)
    assert len(row) == 15
    row = render_status_row({"bpm": 128.7, "bar": 12, "beat": 3,
                             "running": True, "source": "USB MIDI"}, width=200)
    assert len(row) == 200


def test_render_status_row_truncates_to_width():
    row = render_status_row({"bpm": 128.7, "bar": 12, "beat": 3,
                             "running": True, "source": "USB MIDI"}, width=6)
    assert len(row) == 6
    assert row == status_text({"bpm": 128.7, "bar": 12, "beat": 3,
                               "running": True, "source": "USB MIDI"})[:6]


def test_render_status_row_matches_shared_chrome_text_when_it_fits():
    vm = {"bpm": None, "bar": 0, "beat": 1, "running": False, "source": None}
    text = status_text(vm)
    row = render_status_row(vm, width=len(text) + 10)
    assert row.strip() == text


# -- secondary row: alerts/timesig (phase-3 task 6) --------------------------

def test_render_secondary_row_is_exactly_width_wide():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=15)
    assert len(row) == 15


def test_render_secondary_row_shows_timesig_when_no_alerts():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=60)
    assert row.strip() == "Time Signature: (no lock)"


def test_render_secondary_row_prefers_alerts_when_present():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "crit", "held_s": 11.0}]}
    timesig_vm = {"labels": ["4/4"], "confidence": 0.9, "events": 20,
                  "events_window": 20, "events_total": 20, "pending": None}
    row = render_secondary_row(alerts_vm, timesig_vm, width=60)
    assert row.strip() == secondary_status_text(alerts_vm, timesig_vm)
    assert row.strip().startswith("STUCK CRIT:")


def test_render_secondary_row_truncates_to_width():
    alerts_vm = {"alerts": [{"ch": 1, "note": 60, "level": "warn", "held_s": 2.0}]}
    row = render_secondary_row(alerts_vm, DEFAULT_TIMESIG_VM, width=6)
    assert len(row) == 6
    assert row == secondary_status_text(alerts_vm, DEFAULT_TIMESIG_VM)[:6]


def test_render_secondary_row_shows_polylimit_flash_when_no_alerts():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=60,
                               polylimit_vm={"flashing": True})
    assert row.strip() == "POLY LIMIT EXCEEDED"


def test_render_secondary_row_polylimit_param_is_optional_backward_compatible():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=60)
    assert row.strip() == "Time Signature: (no lock)"


def test_render_secondary_row_shows_sysex_status_when_active_and_no_alerts_or_polylimit():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=60,
                               sysex_vm={"text": "sx: rx 4B Roland", "active": True})
    assert row.strip() == "sx: rx 4B Roland"


def test_render_secondary_row_sysex_param_is_optional_backward_compatible():
    row = render_secondary_row(DEFAULT_ALERTS_VM, DEFAULT_TIMESIG_VM, width=60)
    assert row.strip() == "Time Signature: (no lock)"


# -- third chrome row: beatflash/loopprogress (phase-3 task 9) ---------------

def test_render_beatprogress_row_is_exactly_width_wide():
    row = render_beatprogress_row(DEFAULT_BEATFLASH_VM, DEFAULT_LOOPPROGRESS_VM, width=40)
    assert len(row) == 40


def test_render_beatprogress_row_delegates_to_shared_chrome_text():
    beatflash_vm = {"intensity": 1.0, "is_bar": False}
    loopprogress_vm = {"fraction": 0.25, "running": True}
    assert render_beatprogress_row(beatflash_vm, loopprogress_vm, width=50) == \
        beatprogress_row_text(beatflash_vm, loopprogress_vm, 50)


# -- screensaver page (phase-3 task 9) ---------------------------------------

def test_renderers_dispatch_table_has_screensaver():
    assert RENDERERS["screensaver"] is render_screensaver_lines


def test_render_screensaver_lines_is_entirely_blank():
    out = render_screensaver_lines({"title": "SCREENSAVER"}, width=20, height=5)
    assert len(out) == 5
    assert all(line == " " * 20 for line in out)


def test_render_screensaver_lines_handles_zero_height():
    assert render_screensaver_lines({"title": "SCREENSAVER"}, width=20, height=0) == []


def test_screensaver_row_texts_is_fully_blank_including_chrome_rows():
    # IMPORTANT fix (task-9 review): a screensaver-page "golden" proving
    # EVERY row -- header, body, AND all three chrome rows -- goes
    # entirely blank, no reverse-video content anywhere (matching v1's
    # true full-screen fb blank). header/body here are exactly what
    # render_screensaver_lines produces for a 10-row page body.
    header = " " * 40
    body = render_screensaver_lines({"title": "SCREENSAVER"}, width=40, height=10)
    rows = screensaver_row_texts(header, body, width=40)
    assert len(rows) == 1 + 10 + 3   # header + body + secondary/status/beatprogress
    assert all(row == " " * 40 for row in rows)


def test_screensaver_row_texts_chrome_rows_are_blank_even_with_active_content():
    # The chrome-row blanking must not depend on what the (unused) status/
    # alerts/beatflash/etc. VMs would otherwise say -- screensaver_row_texts
    # takes no VM args at all, so there is nothing for "active" content to
    # leak through from. Sanity-check the shape directly.
    rows = screensaver_row_texts("H", ["B1", "B2"], width=5)
    assert rows == ["H", "B1", "B2", "     ", "     ", "     "]


# -- voices page (phase-3 task 4) --------------------------------------------

VOICES_ROWS = [
    {"ch": i, "name": f"Instr{i}", "active": 0, "peak": 0, "notes": []}
    for i in range(1, 17)
]
VOICES_ROWS[0] = {"ch": 1, "name": "Kawai XD5", "active": 3, "peak": 8, "notes": [60, 64, 67]}
VOICES_ROWS[2] = {"ch": 3, "name": "BassStaRack", "active": 12, "peak": 12,
                  "notes": list(range(30, 42))}
VOICES_VM = {"title": "VOICES", "total": 15, "total_peak": 20, "rows": VOICES_ROWS}

# Frozen against an actual run of render_voices_lines(VOICES_VM, 40, 17) --
# the golden text-frame test for this renderer (TUI's equivalent of a golden
# PNG: exact output, not just shape assertions).
GOLDEN_VOICES_FRAME = [
    "VOICES  (poly 15/20)  [n]ext page [q]uit",
    "01 Kawai XD5    ▓▓▓░░░░░  3/8           ",
    "02 Instr2       ░░░░░░░░  0/0           ",
    "03 BassStaRack  ▓▓▓▓▓▓▓▓ 12/12          ",
    "04 Instr4       ░░░░░░░░  0/0           ",
    "05 Instr5       ░░░░░░░░  0/0           ",
    "06 Instr6       ░░░░░░░░  0/0           ",
    "07 Instr7       ░░░░░░░░  0/0           ",
    "08 Instr8       ░░░░░░░░  0/0           ",
    "09 Instr9       ░░░░░░░░  0/0           ",
    "10 Instr10      ░░░░░░░░  0/0           ",
    "11 Instr11      ░░░░░░░░  0/0           ",
    "12 Instr12      ░░░░░░░░  0/0           ",
    "13 Instr13      ░░░░░░░░  0/0           ",
    "14 Instr14      ░░░░░░░░  0/0           ",
    "15 Instr15      ░░░░░░░░  0/0           ",
    "16 Instr16      ░░░░░░░░  0/0           ",
]


def test_voices_render_matches_frozen_golden_frame():
    out = render_voices_lines(VOICES_VM, width=40, height=17)
    assert out == GOLDEN_VOICES_FRAME
    assert all(len(line) == 40 for line in out)


def test_voices_bar_fills_proportionally_and_caps_at_v1_poly_limit_scale():
    # 8 segments == v1's zvoicemonitor.py POLY_LIMIT_CH default -- a fixed
    # visual scale, not an enforced limit (see analyzers/voices.py).
    assert _voices_bar(0) == "░" * 8
    assert _voices_bar(3) == "▓▓▓" + "░" * 5
    assert _voices_bar(8) == "▓" * 8
    assert _voices_bar(20) == "▓" * 8   # capped visually; numeric label stays exact


def test_voices_row_text_shows_true_counts_even_when_bar_is_capped():
    row = {"ch": 3, "name": "BassStaRack", "active": 12, "peak": 12, "notes": []}
    text = render_voices_lines({"title": "VOICES", "total": 12, "total_peak": 12,
                                "rows": [row]}, width=40, height=2)[1]
    assert "12/12" in text
    assert "▓▓▓▓▓▓▓▓" in text   # bar itself is capped at 8 segments


def test_voices_render_truncates_name_to_fixed_width():
    row = {"ch": 1, "name": "A Very Long Instrument Name", "active": 0, "peak": 0, "notes": []}
    out = render_voices_lines({"title": "VOICES", "total": 0, "total_peak": 0, "rows": [row]},
                               width=60, height=2)
    assert "A Very Long " in out[1]
    assert "Instrument" not in out[1]   # truncated to 12 chars, matching name field width


def test_voices_render_pads_blank_rows_at_bottom_when_height_exceeds_row_count():
    row = {"ch": 1, "name": "X", "active": 0, "peak": 0, "notes": []}
    out = render_voices_lines({"title": "VOICES", "total": 0, "total_peak": 0, "rows": [row]},
                               width=20, height=5)
    assert len(out) == 5
    assert out[2] == " " * 20 and out[3] == " " * 20 and out[4] == " " * 20


def test_voices_render_cuts_off_extra_rows_when_height_is_short():
    # Unlike eventlog's newest-at-bottom tail, channels always render in
    # order top-down; a short terminal just loses the highest-numbered rows.
    out = render_voices_lines(VOICES_VM, width=20, height=5)
    assert len(out) == 5
    assert out[1].strip().startswith("01")
    assert out[4].strip().startswith("04")


def test_voices_renderers_dispatch_table_has_voices():
    assert RENDERERS["voices"] is render_voices_lines


# -- harmony page (phase-3 task 5) --------------------------------------------

HARMONY_VM = {
    "title": "HARMONY",
    "chords": [
        {"name": "C maj", "conf": 1.0, "missing": []},
        {"name": "A m", "conf": None, "missing": []},
    ],
    "scales": [
        {"name": "C Ionian", "conf": 0.86, "missing": ["D"]},
        {"name": "A Aeolian 7", "conf": None, "missing": []},
    ],
    "inside": ["C", "E", "G"],
    "outside": ["C#"],
    "key": "C maj",
    "key_conf": 0.83,
    "key_alternatives": ["A min"],
    "tension": 0.35,
    "tension_label": "mild",
    "tension_worst_interval": "M3/m6",
    "harmonic_rhythm": {"changes_per_bar": 1.2, "label": "moderate"},
    "motif": {"found": True, "pattern": "+2 -1 +4", "count": 2},
    "silent": False,
}

# Frozen against an actual run of render_harmony_lines(HARMONY_VM, 60, 13) --
# same "freeze from a real run" discipline as GOLDEN_VOICES_FRAME above.
GOLDEN_HARMONY_FRAME = [
    "HARMONY  (key: C maj)  [n]ext page [q]uit                   ",
    "Chord: Last         2nd          3rd          4th           ",
    "Chord: C maj        A m          --           --            ",
    "Scale: Last         2nd          3rd          4th           ",
    "Scale: C Ionian     A Aeolian 7  --           --            ",
    "Inside: C E G                                               ",
    "Outside: C#                                                 ",
    "Chord conf: 1.00  missing: -                                ",
    "Scale conf: 0.86  missing: D                                ",
    "Key: C maj  (alts: A min)                                   ",
    "Tension: ███████░░░░░░░░░░░░░  0.35  mild  [M3/m6]          ",
    "Harm.rhy: 1.2 ch/bar  moderate                              ",
    "Motif: +2 -1 +4  [x2]                                       ",
]


def test_harmony_render_matches_frozen_golden_frame():
    out = render_harmony_lines(HARMONY_VM, width=60, height=13)
    assert out == GOLDEN_HARMONY_FRAME
    assert all(len(line) == 60 for line in out)


def test_harmony_render_empty_state_shows_placeholders():
    empty = {
        "title": "HARMONY", "chords": [], "scales": [], "inside": [], "outside": [],
        "key": None, "key_conf": 0.0, "key_alternatives": [],
        "tension": 0.0, "tension_label": "silent", "tension_worst_interval": "",
        "harmonic_rhythm": {"changes_per_bar": None, "label": ""},
        "motif": {"found": False, "pattern": None, "count": 0},
        "silent": True,
    }
    out = render_harmony_lines(empty, width=48, height=13)
    assert out[0].startswith("HARMONY  (key: ?)")
    assert "-- " in out[2]           # Chord values row: all placeholders
    assert out[5].strip() == "Inside: -"
    assert out[6].strip() == "Outside: -"
    assert "Chord conf: --  missing: -" in out[7]
    assert "Scale conf: --  missing: -" in out[8]
    assert out[9].strip() == "Key: ?"
    assert "░" * 20 in out[10]        # fully-empty tension bar
    assert out[11].strip() == "Harm.rhy: --"
    assert out[12].strip() == "Motif: --"


def test_harmony_tension_bar_fills_proportionally():
    from midicrt.clients.tui import _harmony_tension_line

    line = _harmony_tension_line({"tension": 0.5, "tension_label": "mild",
                                   "tension_worst_interval": ""})
    assert "█" * 10 in line
    assert "░" * 10 in line


def test_harmony_render_pads_blank_rows_when_height_exceeds_body():
    out = render_harmony_lines(HARMONY_VM, width=20, height=20)
    assert len(out) == 20
    assert out[-1] == " " * 20


def test_harmony_render_cuts_off_extra_rows_when_height_is_short():
    out = render_harmony_lines(HARMONY_VM, width=20, height=3)
    assert len(out) == 3
    assert out[1].startswith("Chord: Last")


def test_harmony_renderers_dispatch_table_has_harmony():
    assert RENDERERS["harmony"] is render_harmony_lines


# -- tuner page (phase-3 task 6) ----------------------------------------------

TUNER_IDLE_VM = {"title": "TUNER", "note": "", "cents": 0.0, "hz": 0.0,
                 "confidence": 0.0, "db": -120.0, "has_signal": False}
TUNER_LOCKED_VM = {"title": "TUNER", "note": "A4", "cents": -3.2, "hz": 439.2,
                   "confidence": 0.82, "db": -18.4, "has_signal": True}

# Frozen against an actual run of render_tuner_lines(TUNER_LOCKED_VM, 60, 5)
# and render_tuner_lines(TUNER_IDLE_VM, 60, 5) -- same "freeze from a real
# run" discipline as GOLDEN_VOICES_FRAME/GOLDEN_HARMONY_FRAME above.
# Re-frozen, fix round (review finding 2): header now carries v1's status
# line (device), see _tuner_header_text's own docstring above.
GOLDEN_TUNER_LOCKED_FRAME = [
    "TUNER  (device: default)  [n]ext page [q]uit                ",
    "Note:A4    Pitch: 439.20 Hz  Cents:  -3.2  Conf:0.82  Level:",
    "Tuning: -------------------^|-------------------            ",
    "                                                            ",
    "                                                            ",
]
GOLDEN_TUNER_IDLE_FRAME = [
    "TUNER  (device: default)  [n]ext page [q]uit                ",
    "Listening...  Conf:0.00  Level:-120.0 dB                    ",
    "                                                            ",
    "                                                            ",
    "                                                            ",
]


def test_tuner_render_matches_frozen_golden_frame_when_locked():
    out = render_tuner_lines(TUNER_LOCKED_VM, width=60, height=5)
    assert out == GOLDEN_TUNER_LOCKED_FRAME
    assert all(len(line) == 60 for line in out)


def test_tuner_render_matches_frozen_golden_frame_when_idle():
    out = render_tuner_lines(TUNER_IDLE_VM, width=60, height=5)
    assert out == GOLDEN_TUNER_IDLE_FRAME
    assert all(len(line) == 60 for line in out)


def test_tuner_render_idle_state_has_no_note_or_meter():
    out = render_tuner_lines(TUNER_IDLE_VM, width=60, height=5)
    assert "Listening" in out[1]
    assert "Note:" not in out[1]
    assert out[2].strip() == ""


def test_tuner_render_locked_state_shows_note_cents_and_meter():
    out = render_tuner_lines(TUNER_LOCKED_VM, width=60, height=5)
    assert "Note:A4" in out[1]
    assert "Cents:  -3.2" in out[1]
    assert "Tuning:" in out[2]
    assert "^" in out[2] and "|" in out[2]


def test_tuner_render_pads_blank_rows_when_height_exceeds_body():
    out = render_tuner_lines(TUNER_IDLE_VM, width=20, height=6)
    assert len(out) == 6
    assert out[-1] == " " * 20


def test_tuner_render_cuts_off_extra_rows_when_height_is_short():
    out = render_tuner_lines(TUNER_LOCKED_VM, width=20, height=2)
    assert len(out) == 2
    assert "Note:" in out[1]


def test_tuner_renderers_dispatch_table_has_tuner():
    assert RENDERERS["tuner"] is render_tuner_lines


# -- tuner page, "no audio input" state (Phase 9 Task 3) ----------------------
#
# Third render state alongside idle/locked above -- mirrors
# tests/test_fb_render.py's own identical TUNER_NO_AUDIO_VM addition; see
# that file's comment for the full "available defaults True" rationale.

TUNER_NO_AUDIO_VM = {"title": "TUNER", "note": "", "cents": 0.0, "hz": 0.0,
                     "confidence": 0.0, "db": -120.0, "has_signal": False,
                     "available": False, "device": None}

# Frozen against an actual run of render_tuner_lines(TUNER_NO_AUDIO_VM, 60, 5)
# -- same "freeze from a real run" discipline as GOLDEN_TUNER_LOCKED_FRAME/
# GOLDEN_TUNER_IDLE_FRAME above.
GOLDEN_TUNER_NO_AUDIO_FRAME = [
    "TUNER  [n]ext page [q]uit                                   ",
    "no audio input                                              ",
    "                                                            ",
    "                                                            ",
    "                                                            ",
]


def test_tuner_render_matches_frozen_golden_frame_when_no_audio():
    out = render_tuner_lines(TUNER_NO_AUDIO_VM, width=60, height=5)
    assert out == GOLDEN_TUNER_NO_AUDIO_FRAME
    assert all(len(line) == 60 for line in out)


def test_tuner_render_no_audio_state_has_no_note_meter_or_listening_text():
    out = render_tuner_lines(TUNER_NO_AUDIO_VM, width=60, height=5)
    assert "no audio input" in out[1]
    assert "Listening" not in out[1]
    assert "Note:" not in out[1]
    assert out[2].strip() == ""


# -- pianoroll page (phase-3 task 7) ------------------------------------------
#
# A FIXED synthetic note set (three notes, spread across pitch rows/velocity
# tiers/an active-vs-closed span) exercised in BOTH projection modes -- the
# renderer itself is mode-agnostic (it only ever reads the already-projected
# x0/x1/y/vel floats, see clients/tui.py's own module comment), so "golden
# per mode" here proves the renderer's HEADER text tracks `window.mode`
# correctly while the body glyphs stay identical (same input geometry).
PIANOROLL_NOTES = [
    {"ch": 1, "y": 0.0, "x0": 0.1, "x1": 0.9, "vel": 1.0, "active": False},
    {"ch": 2, "y": 0.5, "x0": 0.4, "x1": 0.6, "vel": 0.5, "active": False},
    {"ch": 3, "y": 1.0, "x0": 0.7, "x1": 1.0, "vel": 0.2, "active": True},
]
PIANOROLL_VM_WALLCLOCK = {
    "title": "PIANOROLL",
    "notes": PIANOROLL_NOTES,
    "window": {"mode": "wallclock", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0},
    "range": {"lo": 60, "hi": 72},
}
PIANOROLL_VM_TEMPO = {
    **PIANOROLL_VM_WALLCLOCK,
    "window": {"mode": "tempo", "span_s": 8.0, "span_beats": 16.0, "zoom": 1.0},
}

# Frozen against an actual run of render_pianoroll_lines(VM, 48, 8) for both
# VMs above -- same "freeze from a real run" discipline as GOLDEN_VOICES_
# FRAME/GOLDEN_HARMONY_FRAME/GOLDEN_TUNER_*_FRAME.
GOLDEN_PIANOROLL_WALLCLOCK_FRAME = [
    'PIANOROLL  (wallclock zoom 1.00, range 60-72)  [',
    '    ████████████████████████████████████████    ',
    '                                                ',
    '                                                ',
    '                   ▓▓▓▓▓▓▓▓▓▓                   ',
    '                                                ',
    '                                                ',
    '                                 ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒',
]
GOLDEN_PIANOROLL_TEMPO_FRAME = [
    'PIANOROLL  (tempo zoom 1.00, range 60-72)  [n]ex',
    '    ████████████████████████████████████████    ',
    '                                                ',
    '                                                ',
    '                   ▓▓▓▓▓▓▓▓▓▓                   ',
    '                                                ',
    '                                                ',
    '                                 ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒',
]


def test_pianoroll_render_matches_frozen_golden_frame_in_wallclock_mode():
    out = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=48, height=8)
    assert out == GOLDEN_PIANOROLL_WALLCLOCK_FRAME
    assert all(len(line) == 48 for line in out)


def test_pianoroll_render_matches_frozen_golden_frame_in_tempo_mode():
    out = render_pianoroll_lines(PIANOROLL_VM_TEMPO, width=48, height=8)
    assert out == GOLDEN_PIANOROLL_TEMPO_FRAME
    assert all(len(line) == 48 for line in out)


def test_pianoroll_body_glyphs_are_identical_across_projection_modes():
    # The renderer only ever consumes already-projected coordinates -- see
    # module comment above clients/tui.py's render_pianoroll_lines.
    wallclock = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=48, height=8)[1:]
    tempo = render_pianoroll_lines(PIANOROLL_VM_TEMPO, width=48, height=8)[1:]
    assert wallclock == tempo


def test_pianoroll_render_empty_notes_is_all_blank_body():
    empty = {**PIANOROLL_VM_WALLCLOCK, "notes": []}
    out = render_pianoroll_lines(empty, width=20, height=5)
    assert out[1:] == [" " * 20] * 4


def test_pianoroll_render_pads_to_exactly_height_rows():
    out = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=20, height=10)
    assert len(out) == 10
    assert all(len(line) == 20 for line in out)


def test_pianoroll_render_zero_body_height_returns_only_header():
    out = render_pianoroll_lines(PIANOROLL_VM_WALLCLOCK, width=20, height=1)
    assert len(out) == 1


def test_roll_glyph_thresholds_match_v1s_text_renderer_exactly():
    # Ported byte-for-byte from ui/renderers/text/renderer.py::
    # TextRenderer._velocity_char's raw-velocity thresholds (96/127, 48/127).
    assert _roll_glyph(0.0) == " "
    assert _roll_glyph(1 / 127) == "▒"
    assert _roll_glyph(47 / 127) == "▒"
    assert _roll_glyph(48 / 127) == "▓"
    assert _roll_glyph(95 / 127) == "▓"
    assert _roll_glyph(96 / 127) == "█"
    assert _roll_glyph(1.0) == "█"


def test_pianoroll_grid_picks_highest_velocity_on_overlap():
    notes = [
        {"ch": 1, "y": 0.0, "x0": 0.0, "x1": 1.0, "vel": 0.2, "active": False},
        {"ch": 2, "y": 0.0, "x0": 0.0, "x1": 1.0, "vel": 0.9, "active": False},
    ]
    grid = _pianoroll_grid({"notes": notes}, width=4, body_h=2)
    assert grid[0] == [0.9, 0.9, 0.9, 0.9]
    assert grid[1] == [0.0, 0.0, 0.0, 0.0]


def test_pianoroll_grid_zero_dimensions_do_not_crash():
    assert _pianoroll_grid({"notes": []}, width=0, body_h=5) == [[]] * 5
    assert _pianoroll_grid({"notes": []}, width=5, body_h=0) == []


def test_pianoroll_renderers_dispatch_table_has_pianoroll():
    assert RENDERERS["pianoroll"] is render_pianoroll_lines


# -- spectrum page (phase-3 task 8) -------------------------------------------
#
# A FIXED synthetic bins/peak_hold VM (8 bins, a symmetric "hill" shape) --
# same "freeze from a real run" discipline as the other pages' golden
# frames above. Real audio hardware is never touched by this test (or any
# test in this file) -- see analyzers/spectrum.py's module docstring.
SPECTRUM_AVAILABLE_VM = {
    "title": "SPECTRUM", "available": True, "device": "USB Audio Device",
    "bins": [0.0, 0.25, 0.5, 0.75, 1.0, 0.5, 0.25, 0.0],
    "peak_hold": [0.1, 0.4, 0.6, 0.9, 1.0, 0.7, 0.4, 0.1],
}
SPECTRUM_IDLE_VM = {
    "title": "SPECTRUM", "available": False, "device": None,
    "bins": [0.0] * 8, "peak_hold": [0.0] * 8,
}

# Frozen against an actual run of render_spectrum_lines(SPECTRUM_AVAILABLE_VM,
# 24, 8) -- same "freeze from a real run" discipline as GOLDEN_VOICES_FRAME/
# GOLDEN_HARMONY_FRAME/GOLDEN_TUNER_*_FRAME/GOLDEN_PIANOROLL_*_FRAME.
GOLDEN_SPECTRUM_FRAME = [
    "SPECTRUM  (device: USB A",
    "   -█                   ",
    "    █-                  ",
    "  -██                   ",
    " -████-                 ",
    "  ████                  ",
    "-██████-                ",
    " ██████                 ",
]
GOLDEN_SPECTRUM_IDLE_FRAME = [
    "SPECTRUM  [n]ext page [q",
    "no audio input          ",
    "                        ",
    "                        ",
    "                        ",
    "                        ",
    "                        ",
    "                        ",
]


def test_spectrum_render_matches_frozen_golden_frame_when_available():
    out = render_spectrum_lines(SPECTRUM_AVAILABLE_VM, width=24, height=8)
    assert out == GOLDEN_SPECTRUM_FRAME
    assert all(len(line) == 24 for line in out)


def test_spectrum_render_matches_frozen_golden_frame_when_idle():
    out = render_spectrum_lines(SPECTRUM_IDLE_VM, width=24, height=8)
    assert out == GOLDEN_SPECTRUM_IDLE_FRAME
    assert all(len(line) == 24 for line in out)


def test_spectrum_render_idle_shows_no_audio_input_placeholder():
    out = render_spectrum_lines(SPECTRUM_IDLE_VM, width=40, height=5)
    assert out[1].strip() == "no audio input"
    assert all(line.strip() == "" for line in out[2:])


def test_spectrum_render_header_shows_device_when_available():
    out = render_spectrum_lines(SPECTRUM_AVAILABLE_VM, width=60, height=5)
    assert "USB Audio Device" in out[0]


def test_spectrum_render_header_falls_back_to_default_when_no_device_name():
    vm = {**SPECTRUM_AVAILABLE_VM, "device": None}
    out = render_spectrum_lines(vm, width=60, height=5)
    assert "device: default" in out[0]


def test_spectrum_columns_averages_slices_when_downsampling():
    assert _spectrum_columns([0.0, 1.0, 2.0, 3.0], 2) == [0.5, 2.5]


def test_spectrum_columns_empty_values_returns_empty():
    assert _spectrum_columns([], 4) == []


def test_spectrum_bar_rows_fills_bottom_up_proportionally():
    rows = _spectrum_bar_rows(4, 1, [1.0])
    assert rows == ["█", "█", "█", "█"]
    rows = _spectrum_bar_rows(4, 1, [0.0])
    assert rows == [" ", " ", " ", " "]


def test_spectrum_bar_rows_peak_tick_sits_above_a_lower_live_fill():
    rows = _spectrum_bar_rows(4, 1, [0.25], peaks=[1.0])
    column = "".join(row for row in rows)
    assert column[0] == "-"   # peak at the very top row
    assert column[-1] == "█"  # live fill occupies the bottom row


def test_spectrum_bar_rows_peak_tick_never_overwrites_a_live_bar_cell():
    # peak == level (a bar that just hit its own peak) -- the live "█"
    # glyph wins, no separate "-" tick drawn on top of it.
    rows = _spectrum_bar_rows(4, 1, [1.0], peaks=[1.0])
    assert "".join(rows) == "████"


def test_spectrum_bar_rows_leaves_extra_columns_blank_when_wider_than_bins():
    # v1's own choice: a terminal wider than the bin count does not
    # upsample, it just leaves the extra trailing columns blank.
    rows = _spectrum_bar_rows(2, 5, [1.0, 1.0])
    assert all(row[2:] == "   " for row in rows)


def test_spectrum_bar_rows_empty_bins_is_all_blank():
    rows = _spectrum_bar_rows(3, 5, [])
    assert rows == [" " * 5] * 3


def test_spectrum_renderers_dispatch_table_has_spectrum():
    assert RENDERERS["spectrum"] is render_spectrum_lines


# -- img2txtviz page (phase-3 task 10) ---------------------------------------
#
# A tiny hand-picked 2x2 grid (decoupled from the real analyzer's wave math
# -- same "renderer unit-tested against a directly-constructed VM" style as
# the spectrum tests above) exercises the nearest-neighbor upsample from a
# small fixed grid onto a wider/taller terminal body -- see
# analyzers/img2txtviz.py's module docstring for why the grid is fixed-size
# independent of any client's raster.
IMG2TXTVIZ_VM = {
    "title": "IMG2TXT", "active_notes": 3, "energy": 1.23, "vel_splash": 0.45,
    "invert": False, "charset": " .:#",
    "grid": [[0.0, 1.0], [0.25, 0.75]],
}


def test_img2txtviz_grid_lines_nearest_neighbor_upsamples_correctly():
    # width=4 doubles each of the 2 grid columns; body_h=2 maps 1:1 to the
    # 2 grid rows. charset " .:#" -> index 0=' ', 1='.', 2=':', 3='#'.
    # Row 0 (0.0, 1.0) -> idx 0, 0, 3, 3 -> "  ##"
    # Row 1 (0.25, 0.75) -> idx 1, 1, 3, 3 -> "..##" (0.75*4 == 3.0 exactly)
    lines = _img2txtviz_grid_lines(IMG2TXTVIZ_VM, width=4, body_h=2)
    assert lines == ["  ##", "..##"]


def test_img2txtviz_grid_lines_empty_grid_is_all_blank():
    vm = {**IMG2TXTVIZ_VM, "grid": []}
    lines = _img2txtviz_grid_lines(vm, width=5, body_h=3)
    assert lines == ["     "] * 3


def test_render_img2txtviz_lines_header_shows_notes_and_energy():
    out = render_img2txtviz_lines(IMG2TXTVIZ_VM, width=60, height=5)
    assert "notes:03" in out[0]
    assert "energy:1.23" in out[0]
    assert "splash:0.45" in out[0]


def test_render_img2txtviz_lines_header_shows_invert_flag_only_when_set():
    assert "INV" not in render_img2txtviz_lines(IMG2TXTVIZ_VM, width=60, height=5)[0]
    inverted = {**IMG2TXTVIZ_VM, "invert": True}
    assert "INV" in render_img2txtviz_lines(inverted, width=60, height=5)[0]


def test_render_img2txtviz_lines_pads_to_exact_dimensions():
    out = render_img2txtviz_lines(IMG2TXTVIZ_VM, width=10, height=6)
    assert len(out) == 6
    assert all(len(ln) == 10 for ln in out)


def test_img2txtviz_renderers_dispatch_table_has_img2txtviz():
    assert RENDERERS["img2txtviz"] is render_img2txtviz_lines


# -- config page (phase-3 task 10) -------------------------------------------
#
# Plain "label: value" row dump -- see pages/configview.py's module
# docstring for why this is a fixed flat list rather than v1's recursive
# JSON tree/editor.
CONFIG_VM = {
    "title": "CONFIG",
    "config_rows": [
        {"label": "tick_hz", "value": "30"},
        {"label": "pages", "value": "eventlog, voices"},
    ],
    "engine_rows": [
        {"label": "engine_version", "value": "2.0.0.dev0"},
        {"label": "current_page", "value": "eventlog"},
    ],
}


def test_config_body_lines_lists_config_then_engine_sections():
    lines = _config_body_lines(CONFIG_VM)
    assert lines[0] == "-- Config --"
    assert "tick_hz: 30" in lines
    assert "pages: eventlog, voices" in lines
    assert "-- Engine --" in lines
    assert "engine_version: 2.0.0.dev0" in lines
    assert "current_page: eventlog" in lines


def test_render_config_lines_pads_to_exact_dimensions():
    out = render_config_lines(CONFIG_VM, width=40, height=12)
    assert len(out) == 12
    assert all(len(ln) == 40 for ln in out)


def test_render_config_lines_cuts_off_extra_rows_when_height_is_short():
    out = render_config_lines(CONFIG_VM, width=40, height=2)
    assert len(out) == 2   # header + exactly 1 body row


def test_config_renderers_dispatch_table_has_config():
    assert RENDERERS["config"] is render_config_lines


# -- help page (phase-3 task 12, gap ports) -----------------------------------
#
# Same "-- Section --" + "label: value" row dump convention as
# `render_config_lines` -- see pages/help.py's own module docstring for why
# this describe-data reference IS the v1 Help page's parity port, not a
# literal keybinding-list transcription.
HELP_VM = {
    "title": "HELP",
    "page_rows": [
        {"label": "pages (page.goto <name>)", "value": "eventlog, voices"},
    ],
    "action_rows": [
        {"label": "page.goto", "value": "Jump to a named page  (name:str)"},
        {"label": "page.next", "value": "Advance to the next page in the roster"},
    ],
}


def test_help_body_lines_lists_pages_then_actions_sections():
    lines = _help_body_lines(HELP_VM)
    assert lines[0] == "-- Pages --"
    assert "pages (page.goto <name>): eventlog, voices" in lines
    assert "-- Actions --" in lines
    assert "page.goto: Jump to a named page  (name:str)" in lines
    assert "page.next: Advance to the next page in the roster" in lines
    assert "-- Keymap --" not in lines   # HELP_VM has no keymap_rows key at all


# -- keymap section (Phase 5 Task 3, docs/phase5-notes.md cheap-wins bundle) --

HELP_VM_WITH_KEYMAP = {**HELP_VM, "keymap_rows": [
    {"label": "n", "value": "page.next"},
    {"label": "q", "value": "client.quit"},
]}


def test_help_body_lines_lists_keymap_section_last_when_present():
    lines = _help_body_lines(HELP_VM_WITH_KEYMAP)
    assert lines.index("-- Actions --") < lines.index("-- Keymap --")
    assert "n: page.next" in lines
    assert "q: client.quit" in lines


def test_help_body_lines_omits_keymap_section_when_keymap_rows_is_empty():
    vm = {**HELP_VM, "keymap_rows": []}
    assert "-- Keymap --" not in _help_body_lines(vm)


def test_render_help_lines_pads_to_exact_dimensions():
    out = render_help_lines(HELP_VM, width=40, height=12)
    assert len(out) == 12
    assert all(len(ln) == 40 for ln in out)


def test_render_help_lines_cuts_off_extra_rows_when_height_is_short():
    out = render_help_lines(HELP_VM, width=40, height=2)
    assert len(out) == 2   # header + exactly 1 body row


def test_help_renderers_dispatch_table_has_help():
    assert RENDERERS["help"] is render_help_lines


# -- program changes page (phase-3 task 12, gap ports) ------------------------
#
# Byte-for-byte the same layout as `render_lines` -- see pages/
# progchanges.py's own module docstring for why it reuses eventlog's exact
# `{title, count, lines}` VM shape.
PROGCHANGES_VM = {
    "title": "PROGRAM CHANGES",
    "count": 2,
    "lines": [
        {"text": "[10:00:00]  Ch01 → Program 000", "style": "normal"},
        {"text": "[10:00:05]  Ch02 → Program 012", "style": "normal"},
    ],
}


def test_render_progchanges_lines_geometry_and_content():
    out = tui.RENDERERS["progchanges"](PROGCHANGES_VM, width=40, height=5)
    assert len(out) == 5
    assert all(len(line) == 40 for line in out)
    assert "Ch02 → Program 012" in out[-1]


def test_render_progchanges_lines_tails_to_the_newest_when_short():
    out = tui.RENDERERS["progchanges"](PROGCHANGES_VM, width=40, height=2)
    assert len(out) == 2   # header + exactly 1 body row
    assert "Ch02 → Program 012" in out[-1]


def test_progchanges_renderers_dispatch_table_has_progchanges():
    from midicrt.clients.tui import render_progchanges_lines
    assert RENDERERS["progchanges"] is render_progchanges_lines


# -- CC monitor page (phase-3 task 12, gap ports) ------------------------------
CCMONITOR_VM = {
    "title": "CC MONITOR",
    "channels": [{"ch": ch, "recent": []} for ch in range(1, 17)],
}
CCMONITOR_VM["channels"][0]["recent"] = [
    {"cc": 74, "value": 100, "peak": 120}, {"cc": 1, "value": 64, "peak": 64}]


def test_render_ccmonitor_lines_pads_to_exact_dimensions():
    out = tui.RENDERERS["ccmonitor"](CCMONITOR_VM, width=40, height=17)
    assert len(out) == 17
    assert all(len(ln) == 40 for ln in out)
    assert "CC74:100" in out[1]
    assert "CC01:064" in out[1]


def test_render_ccmonitor_lines_cuts_off_extra_rows_when_height_is_short():
    out = tui.RENDERERS["ccmonitor"](CCMONITOR_VM, width=40, height=3)
    assert len(out) == 3   # header + exactly 2 channel rows


def test_ccmonitor_renderers_dispatch_table_has_ccmonitor():
    from midicrt.clients.tui import render_ccmonitor_lines
    assert RENDERERS["ccmonitor"] is render_ccmonitor_lines


# -- CC dashboard page (phase-3 task 12, gap ports) -----------------------------
CCDASHBOARD_VM = {
    "title": "CC DASHBOARD",
    "entries": [
        {"ch": 1, "cc": 74, "value": 100, "peak": 120, "age_s": 0.3, "fresh": True},
        {"ch": 2, "cc": 1, "value": 40, "peak": 64, "age_s": 5.2, "fresh": False},
    ],
}


def test_render_ccdashboard_lines_pads_to_exact_dimensions():
    out = tui.RENDERERS["ccdashboard"](CCDASHBOARD_VM, width=50, height=5)
    assert len(out) == 5
    assert all(len(ln) == 50 for ln in out)
    assert "Ch01 CC074:100" in out[1]
    assert "LIVE" in out[1]
    assert "Ch02 CC001:040" in out[2]
    assert "5.2s" in out[2]


def test_render_ccdashboard_lines_cuts_off_extra_rows_when_height_is_short():
    out = tui.RENDERERS["ccdashboard"](CCDASHBOARD_VM, width=50, height=2)
    assert len(out) == 2   # header + exactly 1 entry row


def test_ccdashboard_bar_scales_with_value():
    from midicrt.clients.tui import _ccdashboard_bar
    assert _ccdashboard_bar(0) == "░" * 20
    assert _ccdashboard_bar(127) == "█" * 20


def test_ccdashboard_renderers_dispatch_table_has_ccdashboard():
    from midicrt.clients.tui import render_ccdashboard_lines
    assert RENDERERS["ccdashboard"] is render_ccdashboard_lines


# -- chord+key page (phase-3 task 12, gap ports) -------------------------------
CHORDKEY_VM = {
    "title": "CHORD+KEY",
    "recent_pcs": ["C", "E", "G"],
    "chords": [
        {"label": "C maj", "pct": 100, "missing": []},
        {"label": "A m", "pct": 67, "missing": ["A"]},
    ],
    "key": {
        "label": "C maj", "pct": 92, "threshold_pct": 72, "ambiguous": False,
        "top": {"label": "C maj", "pct": 92},
        "alternatives": [{"label": "G maj", "pct": 70}],
    },
    "function": "i (T)",
}
CHORDKEY_EMPTY_VM = {
    "title": "CHORD+KEY", "recent_pcs": [], "chords": [],
    "key": {"label": None, "pct": None, "threshold_pct": 72, "ambiguous": True,
            "top": None, "alternatives": []},
    "function": None,
}


def test_chordkey_body_lines_full_layout():
    lines = tui._chordkey_body_lines(CHORDKEY_VM)
    assert lines[0] == "Recent PCs: C E G"
    assert lines[1] == "Chord candidates:"
    assert "1) C maj  100%  missing:-" in lines
    assert "2) A m   67%  missing:A" in lines
    assert "Stabilized key:" in lines
    assert "Key= C maj  92% (thr 72%)" in lines
    assert "alts: G maj 70%" in lines
    assert "Function: i (T)" in lines


def test_chordkey_body_lines_no_stable_key_shows_top_fallback():
    lines = tui._chordkey_body_lines(CHORDKEY_EMPTY_VM)
    assert "(no chord match yet)" in lines
    assert "Key: ?" in lines
    assert "alts: near-threshold / ambiguous" in lines   # no key yet -> ambiguous=True
    assert "Function: ?" in lines


def test_chordkey_body_lines_ambiguous_key_uses_tilde_tag():
    vm = dict(CHORDKEY_VM)
    vm["key"] = dict(CHORDKEY_VM["key"], ambiguous=True)
    lines = tui._chordkey_body_lines(vm)
    assert "Key~ C maj  92% (thr 72%)" in lines


def test_render_chordkey_lines_pads_to_exact_dimensions():
    out = tui.RENDERERS["chordkey"](CHORDKEY_VM, width=40, height=12)
    assert len(out) == 12
    assert all(len(ln) == 40 for ln in out)


def test_render_chordkey_lines_cuts_off_extra_rows_when_height_is_short():
    out = tui.RENDERERS["chordkey"](CHORDKEY_VM, width=40, height=2)
    assert len(out) == 2   # header + exactly 1 body row


def test_chordkey_renderers_dispatch_table_has_chordkey():
    from midicrt.clients.tui import render_chordkey_lines
    assert RENDERERS["chordkey"] is render_chordkey_lines


# -- send notes page (phase-3 task 12, gap ports) -------------------------------
SENDNOTES_VM = {
    "title": "SEND NOTES",
    "device": "midicrt2 Output",
    "channel": 1, "octave": 4, "velocity": 96, "gate_ms": 120, "active": 2,
}


def test_sendnotes_status_text_shows_all_fields():
    text = tui._sendnotes_status_text(SENDNOTES_VM)
    assert "midicrt2 Output" in text
    assert "Ch:01" in text
    assert "Oct:+4" in text
    assert "Vel:096" in text
    assert "Gate:120ms" in text
    assert "Active:2" in text


def test_sendnotes_status_text_shows_not_open_when_device_is_none():
    vm = dict(SENDNOTES_VM, device=None)
    assert "(not open)" in tui._sendnotes_status_text(vm)


def test_render_sendnotes_lines_pads_to_exact_dimensions():
    out = tui.RENDERERS["sendnotes"](SENDNOTES_VM, width=50, height=6)
    assert len(out) == 6
    assert all(len(ln) == 50 for ln in out)


def test_render_sendnotes_lines_cuts_off_extra_rows_when_height_is_short():
    out = tui.RENDERERS["sendnotes"](SENDNOTES_VM, width=50, height=2)
    assert len(out) == 2   # header + exactly 1 body row


def test_sendnotes_renderers_dispatch_table_has_sendnotes():
    from midicrt.clients.tui import render_sendnotes_lines
    assert RENDERERS["sendnotes"] is render_sendnotes_lines


def test_run_tui_survives_page_switch_before_new_topics_snapshot_arrives(monkeypatch):
    """TUI's twin of fb/app.py::_run_device's regression (same phase-3 task
    11 finding, found live against the real daemon): a page_changed event
    can flip state["page"]/state["topic"] before that new topic's own first
    snapshot arrives (delivery can lag "up to 1/max_rate",
    docs/phase2-notes.md). `render_lines` (the eventlog renderer) crashes on
    `vm['count']` the same way `render_frame` did if an unrelated overlay-
    only update (e.g. overlay.status, which ticks independently) triggers a
    repaint against a `vm` that still belongs to the OLD page.

    Run in a background thread with a generous observation window (rather
    than a queued shutdown sentinel): a sentinel present in the inbox from
    the start would be drained in the SAME `drain_latest` batch as the
    page_changed + overlay.status messages below (that function drains the
    whole queue in one non-blocking pass), short-circuiting via ClientError
    before `run_tui` ever reaches the render call this test exists to
    exercise -- confirmed by hand while developing this test. Queuing the
    sentinel only AFTER the observation window avoids that."""
    inbox = queue.Queue()
    inbox.put({"kind": "snapshot", "topic": "page.screensaver", "data": {"title": "SCREENSAVER"}})
    inbox.put({"kind": "event", "name": "page_changed", "data": {"page": "eventlog"}})
    inbox.put({"kind": "snapshot", "topic": "overlay.status",
               "data": {"bpm": 120.0, "bar": 1, "beat": 1, "running": True, "source": "test"}})

    class FakeEngineClient:
        def __init__(self, socket_path):
            pass

        def connect(self):
            pass

        def request(self, cmd):
            return {"data": {"current_page": "screensaver"}}

        def subscribe(self, topics, max_rate):
            pass

        def unsubscribe(self, topics):
            pass

        def start_reader(self):
            return inbox

        def action(self, name, args=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tui, "EngineClient", FakeEngineClient)

    outcome = {}

    def target():
        try:
            outcome["result"] = run_tui("/tmp/unused.sock")
        except BaseException as exc:  # noqa: BLE001 -- capture ANY crash, not just ClientError
            outcome["exception"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=1.0)  # generous: blessed startup + the drain above, Pi3-safe

    assert "exception" not in outcome, (
        f"run_tui crashed on the mismatched page/vm pairing: {outcome.get('exception')!r}")

    # Clean shutdown: now that the observation window has passed (msg2/msg3
    # long since drained), a sentinel is unambiguous.
    inbox.put(None)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "run_tui did not exit after the shutdown sentinel"
    assert outcome.get("result") == 1


def test_run_tui_refetches_keymap_on_keymap_changed_event(monkeypatch):
    """`keymap_changed` (Phase 4 Task 1, docs/phase4-notes.md, emitted by
    the engine's `config.reload` action) must trigger a REAL re-fetch --
    `fetch_keymap`'s own `describe` round trip, not a cached/reused copy
    from connect time. Same scripted-inbox/FakeEngineClient/background-
    thread technique as
    test_run_tui_survives_page_switch_before_new_topics_snapshot_arrives
    above; here the fake's `describe` response changes across calls so a
    growing `describe_calls` count is direct, unambiguous proof a second
    (or third) fetch actually happened rather than merely not crashing."""
    inbox = queue.Queue()
    inbox.put({"kind": "snapshot", "topic": "page.eventlog",
               "data": {"title": "EVENT LOG", "count": 0, "lines": []}})
    inbox.put({"kind": "event", "name": "keymap_changed", "data": {}})

    describe_calls = []

    class FakeEngineClient:
        def __init__(self, socket_path):
            pass

        def connect(self):
            pass

        def request(self, cmd):
            if cmd == "describe":
                describe_calls.append(cmd)
                # Second+ call reports a remapped keymap -- proves this is
                # a genuine re-fetch, not a value cached at connect time.
                keymap = ({"n": "page.prev"} if len(describe_calls) > 1
                         else {"n": "page.next"})
                return {"data": {"current_page": "eventlog", "keymap": keymap}}
            return {"data": {}}

        def subscribe(self, topics, max_rate):
            pass

        def unsubscribe(self, topics):
            pass

        def start_reader(self):
            return inbox

        def action(self, name, args=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(tui, "EngineClient", FakeEngineClient)

    outcome = {}

    def target():
        try:
            outcome["result"] = run_tui("/tmp/unused.sock")
        except BaseException as exc:  # noqa: BLE001 -- capture ANY crash
            outcome["exception"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert "exception" not in outcome, (
        f"run_tui crashed handling keymap_changed: {outcome.get('exception')!r}")
    # 2 at connect (current_page_topic's own describe() + fetch_keymap's),
    # a 3rd from the keymap_changed event's on_event handler re-fetching.
    assert len(describe_calls) >= 3, (
        f"expected a re-fetch after keymap_changed, only saw {len(describe_calls)} describe() calls"
    )

    inbox.put(None)
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "run_tui did not exit after the shutdown sentinel"
