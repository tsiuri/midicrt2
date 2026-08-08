"""Generates tests/fixtures/replay_session.jsonl -- the ONE committed
session fixture Phase 5 Task 2's replay engine is regression-tested
against (see tests/test_replay.py's own "Regression: the ONE checked-in
fixture" section for the tests that consume it). Run manually to
regenerate (`~/midicrt2-venv/bin/python3 tests/fixtures/gen_replay_fixture.py`)
-- NOT a pytest test itself (no `test_` prefix), and not run automatically
by the suite.

Built directly against `engine/capture.py::CaptureSink` (NOT through a real
`Engine` -- this script has no MIDI hardware, no daemon, nothing live to
record from), matching the task brief's own "generated via CaptureSink in a
test-gen script" instruction. Every "event" line uses a fixed synthetic
epoch (`BASE_TS` below) so the recorded MIDI content (and therefore every
analyzer's derived state) is fully reproducible across regenerations.
`CaptureSink.record_action`/`.record_page_changed` stamp their OWN `ts` via
a live `time.time()` call with no way to inject one (this mirrors real
production capture: "capture stamps marks at DISPATCH time", docs/
phase5-notes.md) -- those two mark kinds' `ts` fields will therefore differ
each time this script is re-run; harmless, since replay never paces
playback on an action/page_changed mark's `ts` (only "event" lines drive
the --speed/--instant pacing, see engine/replay.py's own module
docstring), and no test asserts against their literal value.

Session contents (read this before changing anything -- tests/test_replay.py
asserts against several of these facts by name; grep that file for
"FIXTURE_PATH" before changing the shape below)
---------------------------------------------------------------------------
  1. "start" (transport begins running).
  2. 8 "clock_tick" events at a steady 120bpm (2 bars of 4/4).
  3. A C-major triad (60/64/67) on channel 0, held then released --
     voices peak=3 on that channel, drops back to 0.
  4. An ascending C-major scale (60..72, 8 notes) on channel 1, held then
     mostly released, leaving ONE note (72) still sounding -- voices
     total_peak across both channels reaches 8, final total=1. Also seeds
     harmony's key histogram unambiguously toward "C maj" and gives
     analyzers/timesig.py's `MIN_EVENTS=12` estimator enough note-on
     events to actually attempt an estimate.
  5. One "control_change" event (channel 0, controller 7/volume) purely so
     `events_by_type` has more than two distinct keys.
  6. One "sysex" event: a LEGACY `CMD_SWITCH_PAGE` frame targeting v1 page
     id 4 ("ccmonitor") -- `F0 7D 6D 63 01 04 F7` framing, same byte style
     as tests/fixtures/sysex_captures/legacy-switch-page-*.syx. Exercises
     replay's DELIBERATE non-suppression of sysex (see engine/replay.py's
     own module docstring: sysex replays through `_handle` normally, since
     it's ground-truth raw MIDI whose resulting state change needs no
     mark-applier help). Paired here with the "action"/"page_changed"
     marks a live Engine._handle_sysex would ALSO have recorded for this
     exact command (`_sysex_switch_page`'s own two `self._capture.
     record_action(...)`/`self._capture.record_page_changed(...)` calls,
     engine/core.py) -- added here by hand, matching what CaptureSink
     would have produced if this had gone through a real Engine live.
  7. A note_on(channel=1, note=60) AFTER the sysex frame -- deliberately
     reused by test_replay.py's own
     test_replay_fixture_with_configured_binding_does_not_change_the_summary
     as the exact match a scratch bindings.toml test-binds against.
  8. One "action" mark, origin="client", name="page.next" -- simulates a
     human pressing next on the TUI while recording, which leaves NO MIDI
     trace at all -- paired with its own "page_changed" mark (page=
     "harmony") so replay's mark-applier is the ONLY mechanism that can
     ever reproduce this navigation.
  9. One "action" mark, origin="binding:fixture_b1", name="sendnotes.key"
     -- simulates a MIDI binding firing live; replay must COUNT this
     (actions_by_origin) but never re-execute it (no MIDI is sent, no
     sendnotes state changes) -- see engine/replay.py's own module
     docstring for why action marks are counted, not replayed as real
     dispatches.

Exactly THREE distinct action-mark origins appear ("client",
"binding:fixture_b1", "sysex") -- test_replay.py's own
test_replay_fixture_summary_matches_recorded_content asserts this set
literally; add a fourth origin only alongside updating that test.
"""
from __future__ import annotations

import os
import shutil
import tempfile

from midicrt.engine.capture import CaptureSink

BASE_TS = 1_700_000_000.0
FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "replay_session.jsonl")

# mido convention: sysex payload WITHOUT F0/F7 framing (see engine/sysex.py's
# own module docstring). `01` = CMD_SWITCH_PAGE, `04` = v1 page id 4
# ("ccmonitor" per engine/core.py's `_SYSEX_PAGE_ID_MAP`).
_SYSEX_SWITCH_PAGE_CCMONITOR = (0x7D, 0x6D, 0x63, 0x01, 0x04)


def _ev(ts, kind, *, channel=None, data1=None, data2=None, summary="",
        clock_batch_start=None, sysex_data=None):
    """A tiny stand-in for `engine.core.MidiEvent` -- `CaptureSink.
    record_event` only ever reads plain attributes (duck-typed), mirroring
    test_capture.py's own `make_event` helper."""
    attrs = {
        "ts": ts, "source": "Fixture Generator", "type": kind, "channel": channel,
        "data1": data1, "data2": data2, "summary": summary,
        "clock_batch_start": clock_batch_start, "sysex_data": sysex_data,
    }
    return type("FixtureEvent", (), attrs)()


def generate() -> str:
    """Builds the session in a scratch temp directory (CaptureSink always
    owns a whole directory, never a single bare file) and returns the path
    to the ONE `.jsonl` it wrote -- `main()` below copies that file's bytes
    verbatim to `FIXTURE_PATH`."""
    tmp_dir = tempfile.mkdtemp(prefix="midicrt-fixture-gen-")
    sink = CaptureSink(capture_dir=tmp_dir, engine_version="2.0.0.dev0-fixture",
                       instruments=["Fixture Instrument"])
    start_result = sink.start()
    session_id = start_result["session_id"]

    ts = BASE_TS
    sink.record_event(_ev(ts, "start", summary="start"))

    # 2. 8 clock_ticks @ 120bpm (0.5s/beat) -- 2 bars of 4/4.
    batch_start = None
    for _ in range(8):
        ts += 0.5
        sink.record_event(_ev(ts, "clock_tick", clock_batch_start=batch_start,
                              summary="clock_tick"))
        batch_start = ts

    # 3. C-major triad on channel 0: on, on, on, then off, off, off.
    triad_on_ts = ts + 0.02
    for i, note in enumerate((60, 64, 67)):
        sink.record_event(_ev(triad_on_ts + i * 0.02, "note_on", channel=0, data1=note,
                              data2=100, summary=f"note_on ch1 n{note} v100"))
    triad_off_ts = triad_on_ts + 0.5
    for i, note in enumerate((60, 64, 67)):
        sink.record_event(_ev(triad_off_ts + i * 0.02, "note_off", channel=0, data1=note,
                              data2=0, summary=f"note_off ch1 n{note}"))

    # 4. Ascending C-major scale on channel 1: 8 notes on, 7 released (72
    # stays held).
    scale = (60, 62, 64, 65, 67, 69, 71, 72)
    scale_on_ts = triad_off_ts + 0.5
    for i, note in enumerate(scale):
        sink.record_event(_ev(scale_on_ts + i * 0.1, "note_on", channel=1, data1=note,
                              data2=90, summary=f"note_on ch2 n{note} v90"))
    scale_off_ts = scale_on_ts + len(scale) * 0.1 + 0.2
    for i, note in enumerate(scale[:-1]):
        sink.record_event(_ev(scale_off_ts + i * 0.05, "note_off", channel=1, data1=note,
                              data2=0, summary=f"note_off ch2 n{note}"))

    # 5. One control_change (channel 0, CC7=100).
    cc_ts = scale_off_ts + len(scale) * 0.05 + 0.2
    sink.record_event(_ev(cc_ts, "control_change", channel=0, data1=7, data2=100,
                          summary="cc7=100 ch1"))

    # 6. Sysex CMD_SWITCH_PAGE -> "ccmonitor", plus the action/page_changed
    # marks a live Engine._handle_sysex would have recorded for it.
    sysex_ts = cc_ts + 0.3
    sink.record_event(_ev(sysex_ts, "sysex", sysex_data=_SYSEX_SWITCH_PAGE_CCMONITOR,
                          summary="sysex CMD_SWITCH_PAGE ccmonitor"))
    sink.record_action("page.goto", {"name": "ccmonitor"}, "sysex")
    sink.record_page_changed("ccmonitor")

    # 7. The note_on(channel=1, note=60) test_replay.py binds a scratch
    # binding against.
    bind_probe_ts = sysex_ts + 0.3
    sink.record_event(_ev(bind_probe_ts, "note_on", channel=1, data1=60, data2=110,
                          summary="note_on ch2 n60 v110"))

    # 8. Client-origin page.next with NO MIDI trace at all.
    sink.record_action("page.next", {}, "client")
    sink.record_page_changed("harmony")

    # 9. Binding-origin action mark -- must be COUNTED, never re-executed.
    sink.record_action("sendnotes.key", {"key": "z"}, "binding:fixture_b1")

    sink.stop()
    return sink.session_path(session_id)


def main() -> None:
    generated_path = generate()
    shutil.copyfile(generated_path, FIXTURE_PATH)
    shutil.rmtree(os.path.dirname(generated_path), ignore_errors=True)
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]
    print(f"wrote {FIXTURE_PATH} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
