"""THE BURN-IN TRIPWIRE (Phase 8 Task 4 brief): "a test rendering the same
VM at t and t+N seconds (tick-driven) must assert pixel-position DIFFERENCE
for every audit-marked anti-burn-in mover."

This file consolidates that proof, one test per mover, for everything this
task ported that ISN'T already covered by a tripwire living next to its own
render tests:

- **Header marquee** (v1's own "primary anti-burn-in device",
  docs/visual-audit.md §20b) -- its tripwire lives in `test_fb_render.py`
  (`test_burn_in_tripwire_marquee_header_pixels_differ_at_t_and_t_plus_n`),
  co-located with its own render/wiring tests rather than duplicated here;
  referenced, not repeated.
- **Pianoroll active-row tint fade**, **overlap flash**, and the **"Bars"
  timeline strip** (docs/visual-audit.md §9c) -- all three below, each
  exercising the REAL `PianorollState` (not a hand-built vm) ticked at two
  real timestamps, through the REAL `render_pianoroll_frame`.
- **Beatflash** (§20a row 1) -- the REAL `BeatFlashAnalyzer` ticked at two
  points mid-decay, through the REAL `clients/chrome.py::beatflash_glyph`.

Every test below follows the same shape: build real state, `tick(t)`,
capture a render, `tick(t+N)`, capture again, assert pixel/text difference
-- and, where relevant, assert everything ELSE in the frame stayed
identical, isolating the difference to the one mover under test.
"""
import test_fb_render as fbr

from midicrt.clients import chrome
from midicrt.clients.fb import app
from midicrt.clients.fb.surface import Surface
from midicrt.clients.fb.text import load_font
from midicrt.pages.pianoroll import PianorollState


def note_on(ch0, note, vel=100, ts=0.0):
    from midicrt.engine.core import MidiEvent

    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=ch0,
                      data1=note, data2=vel, summary=f"note_on ch{ch0 + 1} n{note} v{vel}")


def note_off(ch0, note, ts=0.0):
    from midicrt.engine.core import MidiEvent

    return MidiEvent(ts=ts, source="USB MIDI", type="note_off", channel=ch0,
                      data1=note, data2=0, summary=f"note_off ch{ch0 + 1} n{note}")


# -- pianoroll active-row tint fade -------------------------------------------

def test_burn_in_tripwire_pianoroll_row_tint_pixels_differ_during_fade():
    s = PianorollState(now=0.0, span_s=8.0, pitch_lo=60, pitch_hi=72)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.handle(note_off(0, 60, ts=0.1))
    s.tick(8.0)   # still (barely) visible -- fade_until = 8.0 + 1.0

    vm_t = s.view_model()
    assert vm_t["row_tint"], "sanity: the tint must actually be armed at t"
    surf_t = Surface(*fbr.PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_t, surf_t)

    s.tick(8.7)   # 0.7s into the 1.0s fade window -- genuinely decayed
    vm_t_plus_n = s.view_model()
    assert vm_t_plus_n["row_tint"][0]["intensity"] < vm_t["row_tint"][0]["intensity"]
    surf_t_plus_n = Surface(*fbr.PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_t_plus_n, surf_t_plus_n)

    assert surf_t.image.tobytes() != surf_t_plus_n.image.tobytes()


# -- pianoroll overlap flash ---------------------------------------------------

def test_burn_in_tripwire_pianoroll_overlap_flash_pixels_differ_across_phases():
    s = PianorollState(now=0.0, span_s=8.0, pitch_lo=60, pitch_hi=60)
    s.handle(note_on(0, 60, vel=100, ts=0.0))
    s.handle(note_on(1, 60, vel=80, ts=1.0))

    # n=2 -> total_phases=3, flash_hz=16.0*0.90=14.4 -- pick two `now`
    # values landing in DIFFERENT phases (period = 1/14.4 ~= 0.0694s).
    period = 1.0 / (16.0 * 0.90)
    s.tick(2.0 + 0.0 * period)
    vm_t = s.view_model()
    surf_t = Surface(*fbr.PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_t, surf_t)

    s.tick(2.0 + 1.0 * period)   # next phase in the 3-phase cycle
    vm_t_plus_n = s.view_model()
    surf_t_plus_n = Surface(*fbr.PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_t_plus_n, surf_t_plus_n)

    assert vm_t["overlap_flash"] and vm_t_plus_n["overlap_flash"]
    assert vm_t["overlap_flash"][0]["ch"] != vm_t_plus_n["overlap_flash"][0]["ch"]
    assert surf_t.image.tobytes() != surf_t_plus_n.image.tobytes()


# -- pianoroll "Bars" timeline strip (+ the underlying paper-grid scroll) -----

def test_burn_in_tripwire_pianoroll_bars_strip_pixels_differ_while_stopped():
    # The core "paper" ruling (Phase 8 Task 3): the grid -- and therefore
    # this strip, which reuses the SAME grid.bar_xs/beat_xs data -- keeps
    # advancing purely from wall-clock time even with the transport fully
    # stopped and zero MIDI activity. Never started at all, mirroring
    # task-3-report.md's own idle-scroll proof.
    s = PianorollState(now=100.0, span_s=8.0, idle_bpm=120.0, pitch_lo=60, pitch_hi=72)
    vm_t = s.view_model()
    surf_t = Surface(*fbr.PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_t, surf_t)

    assert s.tick(105.25) is False   # nothing has ever played -- not "dirty" by the active/closed test
    vm_t_plus_n = s.view_model()
    assert vm_t_plus_n["grid"]["bar_xs"] != vm_t["grid"]["bar_xs"] or \
        vm_t_plus_n["grid"]["beat_xs"] != vm_t["grid"]["beat_xs"]
    surf_t_plus_n = Surface(*fbr.PIANOROLL_SURFACE_SIZE)
    app.render_pianoroll_frame(vm_t_plus_n, surf_t_plus_n)

    # Isolate the difference to the Bars strip band specifically (not just
    # "the image changed somewhere," which the dotted roll-body guide
    # already proves in test_pages_pianoroll.py).
    font = load_font()
    header_h = font.height + 2 * app.HEADER_PAD
    bars_strip_h = app._pianoroll_bars_strip_height(font)
    strip_t = surf_t.image.crop((0, header_h, surf_t.width, header_h + bars_strip_h)).tobytes()
    strip_t_plus_n = surf_t_plus_n.image.crop(
        (0, header_h, surf_t_plus_n.width, header_h + bars_strip_h)).tobytes()
    assert strip_t != strip_t_plus_n


# -- beatflash -----------------------------------------------------------------

def test_burn_in_tripwire_beatflash_glyph_differs_during_decay():
    from midicrt.analyzers.beatflash import FLASH_DURATION_S, BeatFlashAnalyzer

    def transport(kind, ts):
        from midicrt.engine.core import MidiEvent

        return MidiEvent(ts=ts, source="USB MIDI", type=kind, channel=None,
                          data1=None, data2=None, summary=kind)

    def clock_tick(ts, batch_start):
        from midicrt.engine.core import MidiEvent

        return MidiEvent(ts=ts, source="USB MIDI", type="clock_tick", channel=None,
                          data1=24, data2=None, summary="clock_tick", clock_batch_start=batch_start)

    a = BeatFlashAnalyzer()
    a.handle(transport("start", ts=0.0))
    a.handle(clock_tick(ts=0.5, batch_start=None))   # first beat -- flash starts at t=0.5
    a.tick(0.5 + FLASH_DURATION_S * 0.25)   # 25% through the 0.1s decay window
    vm_t = a.view_model()
    glyph_t = chrome.beatflash_glyph(vm_t)

    a.tick(0.5 + FLASH_DURATION_S * 0.75)   # 75% through -- genuinely decayed further
    vm_t_plus_n = a.view_model()
    glyph_t_plus_n = chrome.beatflash_glyph(vm_t_plus_n)

    assert vm_t_plus_n["intensity"] < vm_t["intensity"]
    assert glyph_t != glyph_t_plus_n
