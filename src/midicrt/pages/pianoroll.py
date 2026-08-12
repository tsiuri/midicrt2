"""Pianoroll page: v1's flagship two-projection-mode scrolling note display.

Ported from `~/codex/midicrt/engine/modules/pianoroll_state.py` (344 lines,
the note-interval state machine: active notes keyed by (channel, pitch),
completed spans, idle-scroll fallback) and `~/codex/midicrt/pages/
pianoroll.py` (690 lines, the "beat" mode renderer/config) +
`~/codex/midicrt/pages/pianoroll_exp.py` (1394 lines, the "tempo_relative"
flagship math -- see `_project_session_page`'s use of `engine.memory.
tempo_timeline.TempoTimeline.project_tick`, both READ-ONLY on the Pi). See
also `~/codex/midicrt/docs/tasks/TASK-2026-03-01-variable-tempo-capture-and-
pianoroll-rescaling.md` for the original design intent: "If current BPM is
reduced, notes from before that reduction should look squished. If current
BPM is increased, notes from before that increase should look stretched."

Storage substrate: real timestamps, not v1's synthetic ticks (disclosed, not
an oversight)
---------------------------------------------------------------------------
v1's `PianoRollState` keys everything on an integer MIDI-clock TICK counter
(`active[(ch,pitch)] = (velocity, start_tick)`, `spans = (start_tick,
end_tick, pitch, ch, vel)`) because its column-scroll math needs a tick axis
to advance in `on_tick()`. v2 has no such counter -- `engine/midi_in.py`
(phase-3 task 3) deliberately does NOT expose raw MIDI-clock ticks to
anything; it aggregates 24 pulses into one `clock_tick` MidiEvent per BEAT.
Every MidiEvent (note_on/note_off included) already carries its own
wall-clock `ts` (see `engine/midi_in.py::translate()`), so this port stores
note intervals as `onset_ts`/`release_ts` REAL timestamps directly instead of
reconstructing them from ticks. This is not a simplification of v1's
behavior -- it is MORE precise than v1's own approach for the piece that
matters, see the projection-math section below.

Two projection modes (task-7 brief: "wallclock" and "tempo")
---------------------------------------------------------------------------
- **wallclock**: a note's screen position is a pure function of real elapsed
  time since it sounded (`now - onset_ts`), scaled by a fixed window of
  `span_s` seconds. Tempo has NO effect on this mode at all -- one real
  second is always the same fraction of the visible window, regardless of
  what the MIDI clock is doing. This is the "just show me what actually
  happened, in real time" mode.
- **tempo**: v1's flagship rescaling feature. A note's position is expressed
  in BEATS-ago-under-the-CURRENT-tempo rather than seconds-ago, so a live
  tempo change visibly rescales the ENTIRE history on screen: slower current
  tempo squishes old notes toward "now"; faster current tempo stretches them
  further back. See the math derivation below.

Tempo-relative projection math (read this before touching `_dist`/`_window`)
---------------------------------------------------------------------------
v1's actual formula (traced from `pianoroll_exp.py::_project_session_page`,
NOT `pages/pianoroll.py`'s own older/differently-invoked
`_project_payload_tempo_relative` helper, which passes a different
`anchor_tick` and was not the version exercised by the acceptance-test-
equivalent math below -- `pianoroll_exp.py` is the "flagship"/actively-used
implementation per the task brief):

    proj_tick = TempoTimeline(anchor_tick=session_start).project_tick(
        tick, current_bpm=B, anchor_tick=session_start)
              = session_start + tick_to_seconds(tick) * (B * PPQN / 60)

where `tick_to_seconds(tick)` piecewise-integrates v1's OWN historical tempo
segments to reconstruct "how many real seconds elapsed between session start
and this tick". Concretely, for a constant historical tempo H and PPQN P:
`tick_to_seconds(tick) = tick / (H * P / 60)`, so

    proj_tick = tick * (B / H)

-- i.e. project_tick's ENTIRE effect is "take the real elapsed time this
event represents, and re-express it as if the CURRENT tempo B had been
running the whole time instead of the historical tempo H". Verified by hand
(B=120,H=120 -> identity; B=240,H=120 -> proj_tick=2*tick, i.e. STRETCHED;
B=60,H=120 -> proj_tick=tick/2, i.e. SQUISHED) and cross-checked against
`~/codex/midicrt/tests/test_memory_tempo_projection.py`'s own
`test_tempo_relative_lower_current_bpm_squishes_older_faster_spans` /
`..._higher_current_bpm_stretches_older_slower_spans` (ported below as
`test_tempo_mode_lower_bpm_squishes_history` /
`..._higher_bpm_stretches_history`).

`tick_to_seconds` exists in v1 ONLY to reconstruct real elapsed time from a
tick count, because that's the only thing v1's storage substrate has. v2's
`_Span.onset_ts`/`release_ts` ARE real elapsed wall-clock time already --
`now - onset_ts` is the exact quantity v1 has to approximate via piecewise
historical-tempo segments. So this port's `_dist()` computes the exact v2
equivalent of v1's whole formula in one step, with no segment/timeline
object needed for THIS half:

    beats_ago = (now - onset_ts) * current_bpm / 60.0

(the beats-domain restatement of v1's `tick * (B/H)` -- v2 has no PPQN
sub-beat resolution to preserve, so the natural v2 unit is BEATS, matching
the task-7 brief's "project onto beat-time using the clock_tick timeline"
wording exactly, rather than v1's PPQN ticks). `current_bpm` is the ONE
piece that genuinely needs the live `clock_tick` stream -- see `_advance_
clock()` below, which derives it with the exact same formula `analyzers/
transport.py::TransportAnalyzer` uses (`60 / (ts - clock_batch_start)`),
consuming the SAME clock_tick MidiEvents (independently -- pages and
analyzers each get their own `handle(ev)` call per event off the engine's
shared queue; there is no cross-referencing between the two registries in
`engine/core.py`, so this is a small, deliberate, disclosed duplication of
that one-line formula, not a shared object).

Squish/stretch sanity check for the reader: hold `now - onset_ts` FIXED (a
note that happened N real seconds ago is a fixed fact). If `current_bpm`
increases, `beats_ago` increases -- the note is pushed further from "now" in
beat-space, so a fixed-BEATS-wide window shows relatively MORE spread
between notes of different ages (STRETCHED). If `current_bpm` decreases,
`beats_ago` shrinks toward 0 for everything -- old notes crowd toward "now"
(SQUISHED). Matches the task doc's acceptance criteria exactly.

VM shape (normalized [0,1]^2 layout space per docs/phase3-notes.md; adapted
from the task-7 brief's sketch, finalized against what this port actually
computes)
---------------------------------------------------------------------------
    {"title": "PIANOROLL",
     "notes": [{"ch": int, "y": 0..1, "x0": 0..1, "x1": 0..1,
                "vel": 0..1, "active": bool}, ...],
     "window": {"mode": "wallclock"|"tempo", "span_s": float,
                "span_beats": float, "zoom": float,
                # Phase 10 Task A (items 3+9) additions -- see _window()'s
                # own docstring for the full client-extrapolation derivation.
                "origin_ts": float, "velocity": float, "running": bool},
     "range": {"lo": midi, "hi": midi},
     "grid": {...},   # see "Paper grid" section below
     "row_tint": [{"y": 0..1, "intensity": 0..1}, ...],
     "overlap_flash": [{"y": 0..1, "x0": 0..1, "x1": 0..1,
                         "ch": int | None, "vel": 0..1 | None}, ...]}

- `row_tint`/`overlap_flash` (Phase 8 Task 4): see this module's own
  "Active-row tint + 1s fade-out, and overlap-flash" docstring section
  below for the full v1-mechanics citation. Both are semantic 0..1/
  fraction data, not colors -- the renderer maps them through `clients/
  fb/lum.py`'s ramps, same convention `window`/`range`/`grid` already
  establish.

Client-side extrapolation (Phase 10 Task A, docs/demo-feedback-2026-08-12.md
items 3+9 -- "MERGES with #3" per that doc's own cluster note)
---------------------------------------------------------------------------
Investigation (measured live against a scratch daemon, docs/phase10-task-a-
report.md has the full numbers): at the fb client's own default `--fps 30`,
the engine's `_flush_dirty()`/`ProtocolServer._push_loop` pair deliver a
fresh `page.pianoroll` snapshot roughly every 33-36ms (~28-29/s, matching
`tick_hz=30`) -- but the fb redraw loop (`clients/fb/app.py::_run_device`)
polls on its OWN independent ~33ms clock (`period = 1.0/fps`) and only
repaints `if page_updated` (a NEW snapshot was actually drained THIS tick).
Two independently-phased ~33ms clocks are not synchronized, so ~7% of
redraw ticks find nothing new and silently hold the previous frame for one
extra tick (~33ms) -- occasional, small, but real "jerkiness", and on TOP
of that, motion between any two snapshots is a hard jump (whatever `x0`/`x1`
the ENGINE last computed at ITS tick), never smoothly interpolated -- v1 by
contrast drew LOCALLY at a 30-60fps target, recomputing its own scroll
position from `_frame_dt` every single frame, hence visibly smoother even
at an equivalent or lower underlying MIDI-clock data rate.

The fix: `_window()`'s `origin_ts`/`velocity` (above) let a client advance
EVERY x-coordinate this VM carries -- `notes[].x0` always, `notes[].x1`
ONLY when `not active` (an active/held note's `x1` is pinned at 1.0 by
definition, see the VM-shape bullet above -- extrapolating it too would
incorrectly slide a still-sounding note's right edge leftward), and
`grid.beat_xs`/`grid.bar_xs` -- locally, every render frame, via
`x_now = clamp(x_snapshot + velocity * (client_now - origin_ts), 0, 1)`,
the same clamp `_x()` above already applies server-side. `clients/chrome.
py::extrapolate_pianoroll_vm` is the ONE shared pure implementation both
the fb and TUI clients call (never per-client-duplicated); each client's
OWN render loop is responsible for calling it every frame it repaints,
using its OWN wall-clock `time.time()` read (the SAME clock basis
`origin_ts`/every `onset_ts`/`release_ts` this whole module already uses --
valid because client and engine run on the SAME machine/system clock in
this deployment, not across a network boundary). fb's `_run_device` was
additionally changed to repaint the pianoroll page EVERY tick (not only
`if page_updated`) so the extrapolation is actually visible between real
snapshots -- other pages are UNCHANGED (still snapshot-driven only), so
this task's own CPU-budget constraint ("no material idle-floor regression")
is scoped to time spent ON the pianoroll page, where the measured baseline
was already repainting ~93% of ticks (see investigation numbers above) --
repainting the remaining ~7% is a small, disclosed, page-scoped increment,
not a new always-on cost.

- `y`: 0 at the HIGHEST visible pitch, 1 at the lowest -- matches v1's own
  top-down convention (`pitches = range(pitch_high, pitch_low-1, -1)`, drawn
  top row first). Notes outside `[lo, hi]` are dropped, matching v1's exact
  `if pitch < pitch_low or pitch > pitch_high: continue` (no auto-fit: v1's
  own range is a FIXED default (36..83) whose *high* end is only ever
  adjusted by terminal height or explicit PGUP/PGDN panning, never by note
  content -- there is no v1 "auto-fit to what's playing" behavior to port,
  so this state keeps a fixed default range rather than inventing one).
- `x0`/`x1`: onset/release position, 1.0 = "now" (the right/newest edge,
  matching v1's `tick_right` convention), 0.0 = the window's oldest visible
  instant. A still-held note (`active: true`) always has `x1 == 1.0`
  (`release_ts` treated as `now`). Notes that have scrolled entirely past
  the left edge are dropped, not merely clamped to x=0 -- matches v1's own
  page-window membership filter (`if proj_e < page_start: continue`).
- `window.span_s`/`span_beats`: UNLIKE the task brief's sketch (`"span_
  beats"|"span_s"`, implying "whichever matches `mode`"), this VM always
  reports BOTH, one derived from the other via `current_bpm` -- e.g. in
  "wallclock" mode `span_beats = span_s * bpm / 60` is a secondary, cosmetic
  read-out, not the mode's own driver. This follows the same "always a
  dict/always both fields present" convention `pages/harmony.py` established
  for `harmonic_rhythm`/`motif` (a renderer never has to branch on which key
  exists, only which `mode` string is active) rather than a `TypedDict`-style
  either/or.
- `zoom`: a NEW control, not a literal v1 field -- v1's roll width was
  implicitly `roll_cols * ticks_per_col` derived from the terminal's own
  column count (a renderer/display-geometry concern); v2's engine-side VM
  has no terminal width to borrow, so "how much history is visible" has to
  be an engine-owned, explicitly adjustable quantity for the `pianoroll.
  zoom` action (task-7 brief) to act on. `zoom > 1` narrows the effective
  window (more detail, less history); `zoom < 1` widens it.

Not ported (disclosed, not silently dropped)
---------------------------------------------------------------------------
- **Out-of-range overflow indicators** (v1's `last_above`/`last_below`,
  "C6 ch03 (+2 more)" footer hints for notes currently outside the visible
  pitch range). The task-7 brief's VM contract has no field for this, and
  it is a footer/HUD concern layered on top of the core roll, not part of
  the state/projection core this task ports.
- **`recent_hits` short-lived onset overlay** (v1 shows a just-struck note
  for one extra "logical column" beyond its real duration so fast staccato
  hits remain visible against a coarse column grid). v2's normalized
  x0/x1 are continuous floats, not discretized into columns until the
  renderer quantizes them (docs/phase3-notes.md's own "renderers only
  quantize" rule) -- a renderer with a coarse cell grid (the TUI) already
  gets a minimum-one-cell-wide visual for any real span for free, so this
  v1 workaround for column-quantization has no v2 equivalent to port.
- **Idle-scroll "column stepping" (`MAX_STEPS_PER_FRAME`, `_sample_offsets`,
  `cols_buf`)**: v1 incrementally builds a scrolling COLUMN BUFFER once per
  UI-poll tick and must cap/sample how many columns it advances per frame to
  avoid a huge backlog after a slow frame. v2's `view_model()` recomputes
  the whole visible window FRESH from `_active`/`_closed` on every call
  (the same "pull, don't incrementally accumulate" architecture every other
  v2 page already uses) -- there is no column buffer to step, so this whole
  v1 subsystem has no v2 analogue. `history_retention_s` pruning
  (`_prune()`) is this port's equivalent safeguard against unbounded memory
  growth, not a frame-budget concern.
- **PIXEL_STYLE (text/dense) toggle**: v1's single TUI renderer could switch
  glyph density; v2 already has fully separate TUI/fb renderers (this
  task's own deliverable), so there is no single-renderer "style" knob to
  toggle.
- **Pitch-range panning (PGUP/PGDN/arrow keys)**: v1 lets the user shift
  `pitch_low`/`pitch_high` interactively. The task-7 brief's action list is
  `pianoroll.zoom`/`pianoroll.projection` plus "any v1 controls you find,
  e.g. channel filter" -- channel filter is ported (`pianoroll.channels`,
  below); range panning is a second, independent v1 control this task
  discloses but does not add an action for (no VM field would need to
  change beyond `range.lo`/`range.hi`, which are otherwise fixed constants
  here -- straightforward future work, not done here to keep this task's
  action surface to what the brief actually names).

Ported v1 controls
---------------------------------------------------------------------------
- **Channel visibility** (v1 `pages/pianoroll.py`'s `v`/`apply_visibility_
  list` keybinding: comma-separated channel numbers and `a-b` ranges, empty
  = show all). Ported as the `pianoroll.channels` action / `set_channels()`
  below, with ONE deliberate behavior change: v1's `apply_visibility_list`
  silently falls back to "show all" on ANY parse error (`except Exception:
  visible_channels.update(range(1,17))`) -- a reasonable default for a
  typo-tolerant interactive keystroke-at-a-time text-entry widget. v2's
  action interface is a programmatic/scripted call, not a live keystroke
  buffer; silently reinterpreting a caller's malformed input as "show
  everything" is a worse failure mode there than a clear `ActionError`, so
  `set_channels()` raises `ValueError` (turned into `ActionError` by
  `engine/core.py`) on a malformed spec instead. Empty string still means
  "show all", matching v1.
- **CC 123 (All Notes Off) closes every active note ON THAT CHANNEL ONLY**
  -- ported byte-for-byte from `pianoroll_state.py::on_midi_event`'s
  `if kind == "control_change" and int(getattr(msg, "control", -1)) == 123`.
  This is a deliberate, notable DIVERGENCE from `analyzers/voices.py`'s and
  `analyzers/stucknotes.py`'s own CC handling, both of which react to
  `{120, 123}` (All Sound Off ALSO clears there) -- v1's pianoroll state
  genuinely only listens for 123, not 120, and this port preserves that
  quirk rather than "fixing" it to match the other two analyzers, per this
  project's "port faithfully, disclose don't silently normalize" convention
  (see `pages/harmony.py`'s own chord-name-quirk precedent).
- **"start" resets everything; "stop" closes but does not clear; "continue"
  is a true no-op** -- ported from `pianoroll_state.py::on_midi_event`'s
  "start"/"stop" branches exactly (`_reset_transport_history()` on start;
  closing every active span into `spans` on stop, WITHOUT clearing
  `spans`/history). "continue" has no explicit v1 branch in
  `on_midi_event` at all (falls through to nothing) -- ported as a genuine
  no-op here too.

Paper grid (Phase 8 Task 3, docs/visual-audit.md §9c's "build-priority #2")
---------------------------------------------------------------------------
The task's own flagship visual: "a faint low-opacity grid, synced to
play/pause and BPM, fixed almost like paper scrolling across the screen."
v1's exact mechanism (audit-confirmed, `compositor_renderer.py::
_render_pianoroll`'s dotted-guide block, fed by `pianoroll_state.py::
on_tick`'s dual-clock scroll): horizontal dotted separators between pitch
rows (brighter on C rows), dotted vertical guides at bar/beat tick
boundaries computed from the SAME `tick_left`/`tick_right_edge` anchor the
note spans use -- so grid and notes are always in registration, and the
grid keeps advancing via wall-clock-at-`idle_scroll_bpm` even while the
transport is stopped (the literal "paper" feel).

`view_model()["grid"]` ports this as pure data (task brief: "the renderer
can draw without doing music math"):

    {"beat_xs": [0..1, ...], "bar_xs": [0..1, ...],
     "pitch_guide_ys": [{"y": 0..1, "is_c": bool, "pitch": int, "name": str}, ...],
     "running": bool}

- `beat_xs`/`bar_xs`: x-fractions of beat/bar boundaries currently inside
  the visible window, using the EXACT SAME `_x()`/`_dist()`/`_current_bpm()`
  projection the notes above already go through -- a beat boundary and a
  note onset at the same real instant always land at the same x, in
  EITHER mode, automatically (no grid-specific mode branch needed). Bar
  boundaries (every `BEATS_PER_BAR`-th beat, imported from `analyzers/
  transport.py` rather than re-declared) are reported separately and are
  mutually exclusive with `beat_xs` -- mirrors v1's own timeline-strip loop
  (`if t % bar_ticks != 0:` before adding a beat tick), so a renderer never
  double-draws the same column.
- `pitch_guide_ys`: one entry per semitone in `[pitch_lo, pitch_hi]`,
  top-down (matches `notes[]`'s own "y: 0 at the highest visible pitch"
  convention) -- `y` for pitch `p` is the IDENTICAL `_y(p)` expression a
  note at that pitch gets, so a renderer can match "does this row have a
  visible note" by plain set membership on `y`, no epsilon needed (see
  `test_grid_pitch_guide_y_matches_note_y_exactly`). `pitch`/`name` are a
  disclosed EXTENSION beyond the task brief's minimal
  `{y, is_c}` sketch -- the brief's own "renderer can draw without doing
  music math" goal applies to the WHOLE grid dict, and the label column
  needs a note name per row; computing `NOTE_NAMES[pitch % 12]` +
  octave in the renderer would be exactly the kind of music math the brief
  says the grid should spare it, so it's precomputed here instead (`NOTE_
  NAMES` imported from `analyzers/theory.py`, the same table `pages/
  chordkey.py` already reuses, rather than re-declared).
- `running`: `self._running`, plain passthrough. **Review fix, disclosed
  choice**: an earlier draft of this docstring claimed this field "lets a
  renderer show a different chrome cue for genuinely-playing vs. idle-
  scrolling" -- no renderer actually does that, and none is added here.
  Two reasons this stays a plain unconsumed passthrough rather than wiring
  a new visual cue: (1) v1's own dotted-guide draw code has NO running/
  stopped branch either (confirmed by the full `_render_pianoroll` read
  this task's docstring already cites elsewhere) -- a v2-only cue here
  would be an unrequested embellishment beyond the "port faithfully"
  scope, not a gap; (2) `clients/fb/app.py`'s existing bottom status strip
  (`_draw_status`, fed by the SEPARATE `overlay.status` topic, not this
  page's own VM) already shows `RUN`/`STOP` for every page including this
  one -- a second, redundant indicator sourced from `grid["running"]`
  specifically would duplicate chrome that already exists. Kept in the VM
  regardless (matches the task brief's literal schema, and costs nothing
  to report) in case a future task finds a genuine non-redundant use for
  it, but the claim of an existing consumer is removed.

Adapted, not a literal port: the anchor mechanism
---------------------------------------------------------------------------
v1 keeps one monotonic INTEGER tick counter (reset to 0 on transport
"start", advanced by real ticks while running or extrapolated wall-clock
"virtual ticks" while stopped -- see the "Fixed like paper" scroll row in
the module's own class docstring reference, `pianoroll_state.py::on_tick`)
and finds bar/beat boundaries by integer modulo on that counter. v2 has no
such counter (this port's storage substrate is real timestamps, not
ticks -- see this module's OWN docstring intro). Instead: `_beat_zero_ts`
is a single real timestamp anchor -- the last transport-"start" event's
`ts`, or this state's own construction time if the transport has never
started (mirrors v1's `last_run_bpm` defaulting to `idle_scroll_bpm`
before any run: the paper is already scrolling from boot, not frozen until
the first "start"). `_grid_instants()` walks backward from `_now` in steps
of `60/_current_bpm()` seconds relative to that anchor, yields each
instant's SIGNED integer offset `m` (for `m % BEATS_PER_BAR == 0` bar
detection) and its projected x -- stopping once `_dist()` exceeds the
active projection's own `_span()`, so zoom/mode/bpm all apply automatically
via the same functions notes use, with NO grid-specific window math
duplicated.

This makes the SAME simplification `_dist()` already discloses for note
projection (module docstring above, "Tempo-relative projection math"):
every boundary is re-projected using the CURRENT bpm uniformly, not v1's
own per-historical-tempo-segment tick integration. A live tempo change
therefore rescales grid spacing exactly like it rescales note spacing (by
design -- see `test_grid_tempo_mode_spacing_differs_from_wallclock_at_
same_bpm`), but a boundary from BEFORE a tempo change won't stay
phase-locked to where v1's real MIDI clock would have put it at the time.
Accepted for the reasons `_dist()`'s own docstring already gives: v2 has
no per-historical-segment integration to draw on, and matching notes'
existing behavior keeps grid and notes visually consistent with each
other, which matters far more here than exact fidelity to a mechanism v1
itself only needed because ticks were its only available unit.
`_GRID_MAX_INSTANTS` bounds the walk defensively (a malformed/adversarial
`clock_tick` pair could otherwise derive an extreme bpm and loop far
longer than any real session's window could ever need -- generous enough
that no plausible bpm/zoom/span combination in this module's own
documented ranges gets clipped).

Active-row tint + 1s fade-out, and overlap-flash (Phase 8 Task 4,
docs/visual-audit.md §9c) -- the two per-frame ANIMATIONS Task 3 deferred
---------------------------------------------------------------------------
Both read from `~/codex/midicrt/fb/compositor_renderer.py::_render_pianoroll`
(the same function Task 3 already read in full) -- specifically the
"Pitches with any visible note data" block (lines 772-825) and the
"Overlap flash pass" block (lines 933-1000).

**Row tint + fade**: v1's `highlight_pitches` ("any span/column entry with
`velocity>0` whose tick range overlaps the current visible window" --
lines 772-793) is EXACTLY this port's own `_visible_spans()` filter
(channel + pitch-range + window-membership) -- the same set `notes[]`
itself is already built from, since v2's `notes[]` is already "spans
currently visible in the window" by construction (task-3-report.md's own
citation of this equivalence for the label-column invert). `tick(now)`
refreshes `_row_fade_until[pitch] = now + _ROLL_ROW_FADE_S` for every
CURRENTLY visible pitch (mirrors v1's own `fade_until = now_mono +
_ROLL_ACTIVE_ROW_FADE_S` refresh, lines 795-798) and drops any expired,
no-longer-visible entry -- `view_model()`'s `row_tint` field then reports
one `{"y", "intensity"}` per still-tinted pitch: `intensity=1.0` while
visible, else `(fade_until - now) / _ROLL_ACTIVE_ROW_FADE_S` (linear decay
to 0, matching v1's own `frac` calculation exactly -- v1 quantizes this
into a 64-step LUT (`_roll_row_fade_lut`) purely as a rendering-side
color-precompute optimization; this port reports the CONTINUOUS fraction
and lets the renderer map it through `clients/fb/lum.py::lum()` instead,
a disclosed strict-precision improvement over v1's own quantization, same
class of change as `analyzers/beatflash.py`'s continuous-decay vs. v1's
binary flag). The 15%-peak-brightness tint color itself
(`_ROLL_ACTIVE_ROW_BASE_RGB = (0, 38, 12)`) is a RENDERER concern, not
reported here -- see `clients/fb/app.py`'s own row-tint section (this VM
reports a plain semantic 0..1 intensity, matching every other continuous
ramp in this codebase's "VM carries the semantic value, renderer maps it
through a named ramp" convention, e.g. img2txtviz's cell grid).

**Overlap flash**: v1's per-pixel interval sweep (lines 941-1000) is
ported here in FRACTION space instead of pixel space -- v2's `notes[]`
already carries continuous `x0`/`x1` fractions through the SAME `_x()`
projection notes and the grid both use, so "do two notes on the same
pitch overlap" is a property of that already-computed data, not a
rendering-resolution artifact the way v1's own "drawn pixel intervals"
comment implies it needed to be. `_overlap_flash()` groups `notes[]` by
`y` (two different pitches never visually overlap on the same row) and
runs v1's exact sweep-line algorithm per group (`_overlap_regions_for_row`,
a module-level pure function): sort start/end events, track the active
set, and for every sub-interval where >=2 notes are simultaneously active,
emit ONE region tagged with EITHER a note's own `(ch, vel)` (so the
renderer can reuse `_roll_note_color`) or `(None, None)` -- the "blink to
BG" phase v1's own `total_phases = n + 1` reserves. Phase selection
(`int(now * flash_hz) % total_phases`) uses THIS state's own `self._now`
(the same injected wall-clock every other continuous animation in this
class already uses -- `_beat_zero_ts`'s anchor, `_row_fade_until`'s
timestamps) rather than v1's `time.monotonic()` -- both are arbitrary
absolute wall-clock references only ever used for a MODULO phase
calculation, so the epoch difference is immaterial; a disclosed adaptation
for consistency with this class's own single-clock-domain convention, not
a v1 mismatch. The `_FLASH_HZ=16.0` base rate and the count-based
multiplier table (`0.90`/`1.30`/`1.70` for 2/3/4+ simultaneous notes) are
byte-for-byte v1's own tuned constants. **Not needed here**: v1's own
"deduplicate same note press" step (`seen_keys` keyed by `(orig_start,
ch)`) exists because v1's pixel-space drawing could register the SAME
logical note press more than once in `overlap_drawn`; v2's `notes[]` has
exactly ONE entry per `_Span` already (this module's own storage
substrate), so there is no duplicate-press artifact to de-duplicate.

**The separate solid "Bars" timeline strip** (§9c: one pixel-ROW of solid
GREEN_MID/GREEN_DIM ticks, distinct from the dotted guides Task 3 ports)
needs NO engine-side change at all -- it reuses this module's existing
`grid.bar_xs`/`grid.beat_xs` data verbatim (a renderer-only addition, see
`clients/fb/app.py`'s own module comment for where it lands).
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from midicrt.analyzers.theory import NOTE_NAMES
from midicrt.analyzers.transport import BEATS_PER_BAR

if TYPE_CHECKING:
    # Type-only: mirrors analyzers/transport.py's own comment -- avoids a
    # circular import with engine.core, which builds _PAGE_FACTORIES from
    # modules like this one.
    from midicrt.engine.core import MidiEvent

# -- v1-derived defaults (pages/pianoroll.py's own module-level constants) --
PITCH_LOW_DEFAULT = 36     # C2
PITCH_HIGH_DEFAULT = 83    # v1's PITCH_HIGH_DEFAULT (comment says "B5"; kept
                           # byte-for-byte, not re-derived -- see module
                           # docstring's "not auto-fit" note)
IDLE_BPM_DEFAULT = 120.0   # v1's IDLE_SCROLL_BPM

# -- v2-native tunables (no direct v1 analogue -- see module docstring's
# "zoom" / "idle-scroll column stepping" notes) --------------------------
DEFAULT_SPAN_S = 8.0
DEFAULT_SPAN_BEATS = 16.0       # 4 bars at 4/4, matching analyzers/
                                 # transport.py's BEATS_PER_BAR=4 convention
ZOOM_MIN = 0.25
ZOOM_MAX = 4.0
ZOOM_DEFAULT = 1.0
# Generously larger than any practical span/zoom combination (worst case:
# DEFAULT_SPAN_S / ZOOM_MIN = 32s, or DEFAULT_SPAN_BEATS / ZOOM_MIN beats at
# a very slow current bpm) so switching mode or zooming out never reveals a
# gap left by over-eager pruning.
HISTORY_RETENTION_S = 120.0

N_CHANNELS = 16
_ALL_NOTES_OFF_CONTROL = 123   # v1 quirk: only 123, not 120 -- see module docstring
_PROJECTION_MODES = ("wallclock", "tempo")

# -- paper-grid tunables (Phase 8 Task 3, see module docstring's "Paper
# grid" section) -----------------------------------------------------------
# Defensive cap on `_grid_instants()`'s backward walk -- see that section's
# closing paragraph for why only wallclock mode's line COUNT can scale with
# bpm at all (tempo mode's count is always bounded by `_effective_span_
# beats()`, currently <=64 given this module's own ZOOM_MIN/DEFAULT_SPAN_
# BEATS). Sized well above the worst plausible wallclock case (DEFAULT_
# SPAN_S / ZOOM_MIN=32s of window at an implausibly fast 10,000bpm derived
# bpm is ~5333 instants) so no realistic bpm/zoom/span combination is ever
# clipped -- this only bites a malformed/adversarial clock_tick pair.
_GRID_MAX_INSTANTS = 8192

# -- active-row tint + fade tunables (Phase 8 Task 4, module docstring's own
# "Active-row tint + 1s fade-out" section) -- v1's exact
# `_ROLL_ACTIVE_ROW_FADE_S` (`compositor_renderer.py:85`).
_ROLL_ROW_FADE_S = 1.0

# -- overlap-flash tunables (Phase 8 Task 4, module docstring's own
# "Overlap flash" section) -- v1's exact `_FLASH_HZ` (`compositor_renderer.py:940`)
# and count-based speed multipliers (`compositor_renderer.py`'s inline
# comment: "slowest: 70%, 2-note: 90%, 3-note: 130%, 4+-note: 170%").
_FLASH_HZ = 16.0
_FLASH_MULT_2 = 0.90
_FLASH_MULT_3 = 1.30
_FLASH_MULT_4_PLUS = 1.70
_FLASH_MULT_DEFAULT = 0.70   # v1's own "<2 concurrently active" branch --
                             # effectively unreachable, flash only starts at 2


def _flash_mult(n: int) -> float:
    """v1's count-based overlap-flash speed multiplier (module docstring's
    "Overlap flash" section) -- `n` is the number of simultaneously-active
    notes at the sweep position being evaluated."""
    if n >= 4:
        return _FLASH_MULT_4_PLUS
    if n == 3:
        return _FLASH_MULT_3
    if n == 2:
        return _FLASH_MULT_2
    return _FLASH_MULT_DEFAULT   # unreachable in practice, see module docstring


def _overlap_regions_for_row(notes_on_row: list[dict], now: float) -> list[dict]:
    """v1's exact overlap-flash sweep-line (module docstring's "Overlap
    flash" section), ported from PIXEL space to FRACTION space: `notes_on_
    row` is a list of `{"x0", "x1", "ch", "vel"}` dicts already sharing the
    same pitch row (the caller groups by `y` first). Returns one region per
    sub-interval where >=2 notes' `[x0, x1)` ranges overlap: `{"x0", "x1",
    "ch", "vel"}` naming the note whose color is "on" this instant, or
    `{"x0", "x1", "ch": None, "vel": None}` for the "blink to BG" phase
    (`phase_idx == n`, v1's `total_phases = n + 1`). Fewer than 2 notes on
    the row can never overlap -- returns `[]` immediately, matching v1's
    own `if len(intervals) < 2: continue` guard."""
    if len(notes_on_row) < 2:
        return []
    events: list[tuple[float, int, int]] = []
    for i, note in enumerate(notes_on_row):
        events.append((note["x0"], 1, i))   # start
        events.append((note["x1"], 0, i))   # end
    events.sort()
    active: set[int] = set()
    prev_x: float | None = None
    regions: list[dict] = []
    for x, etype, idx in events:
        if prev_x is not None and len(active) >= 2 and x > prev_x:
            active_sorted = sorted(active)
            n = len(active_sorted)
            flash_hz = _FLASH_HZ * _flash_mult(n)
            phase_idx = int(now * flash_hz) % (n + 1)
            if phase_idx < n:
                note = notes_on_row[active_sorted[phase_idx]]
                regions.append({"x0": prev_x, "x1": x, "ch": note["ch"], "vel": note["vel"]})
            else:
                regions.append({"x0": prev_x, "x1": x, "ch": None, "vel": None})
        if etype == 1:
            active.add(idx)
        else:
            active.discard(idx)
        prev_x = x
    return regions


@dataclass
class _Span:
    ch: int
    pitch: int
    vel: int
    onset_ts: float
    release_ts: float | None = None   # None while the note is still held


def _parse_channel_spec(text: str) -> set[int]:
    """Port of v1's `apply_visibility_list` parsing rules (comma-separated
    channel numbers and `a-b` inclusive ranges, 1..16, empty = all) -- see
    module docstring's "Ported v1 controls" for the one behavior change
    (raises `ValueError` on malformed input instead of v1's silent
    fall-back-to-all)."""
    text = (text or "").strip()
    if not text:
        return set(range(1, N_CHANNELS + 1))
    channels: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = sorted((int(a), int(b)))
            channels.update(c for c in range(lo, hi + 1) if 1 <= c <= N_CHANNELS)
        else:
            c = int(part)
            if 1 <= c <= N_CHANNELS:
                channels.add(c)
    if not channels:
        raise ValueError(f"channel spec selects no valid channel (1-{N_CHANNELS}): {text!r}")
    return channels


class PianorollState:
    """Pure(ish) state machine: `handle(MidiEvent) -> bool` (dirty),
    `tick(now) -> bool` (dirty; wall-clock scroll-progress hook, mirrors
    `analyzers/stucknotes.py`'s `tick()` -- see `engine/core.py`'s
    `_tick_pages`), `view_model() -> dict`. `now` is tracked internally
    (updated from `ev.ts`/`tick()`'s injected `now`, never read via
    `time.time()` inside `handle`/`tick`/`view_model` themselves) except for
    the ONE-TIME constructor default, the same accepted "at construction"
    clock read `engine/core.py::Engine.__init__`'s own `self.started_at`
    already uses.
    """

    def __init__(self, pitch_lo: int = PITCH_LOW_DEFAULT, pitch_hi: int = PITCH_HIGH_DEFAULT,
                 span_s: float = DEFAULT_SPAN_S, span_beats: float = DEFAULT_SPAN_BEATS,
                 idle_bpm: float = IDLE_BPM_DEFAULT, now: float | None = None) -> None:
        self._pitch_lo = int(pitch_lo)
        self._pitch_hi = int(pitch_hi)
        self._span_s = float(span_s)
        self._span_beats = float(span_beats)
        self._idle_bpm = float(idle_bpm)
        self._zoom = ZOOM_DEFAULT
        # Phase 10 Task A (docs/demo-feedback-2026-08-12.md items 3+9):
        # DEFAULT FLIPPED from "wallclock" to "tempo" -- v1-parity fix, not
        # a preference change. v1's OWN default pianoroll behavior
        # (`~/codex/midicrt/pages/pianoroll.py:22`, `PROJECTION_MODE =
        # "beat"`) advances its scroll by TICK COLUMNS that arrive at the
        # live MIDI clock's real-time rate (`engine/modules/pianoroll_
        # state.py::on_tick`, `moved = tick - self.last_tick` where `tick`
        # is the real, wall-clock-paced 24-ppqn counter) while each column
        # always represents a FIXED tick count (`TICKS_PER_COL`, constant
        # beat-fraction) -- i.e. v1's actual live behavior is EXACTLY this
        # module's "tempo" mode's own property (constant-ish division
        # width, paper velocity proportional to tempo), not "wallclock"'s
        # (constant real-time speed, tempo only reshapes the grid's
        # cosmetic spacing). The user's item-9 report ("BPM changes bar-
        # division WIDTH while scroll speed stays constant") is a literal
        # description of "wallclock" mode as experienced live -- v2 shipped
        # with the WRONG default relative to v1, an inverted-from-v1
        # choice this task task-7's own original port never called out as
        # deliberate. "wallclock" mode itself is UNCHANGED and still fully
        # available (`pianoroll.projection`/`.projection_toggle` actions,
        # `p` keybind, `engine/keymap.py:349`) -- this is a default-VALUE
        # fix, not a feature removal.
        self._projection = "tempo"
        self._visible_channels: set[int] = set(range(1, N_CHANNELS + 1))
        self._active: dict[tuple[int, int], _Span] = {}
        self._closed: deque[_Span] = deque()
        self._bpm: float | None = None
        # Own, independent running-gate for clock_tick bpm derivation --
        # NOT shared with analyzers/transport.py's TransportAnalyzer (pages
        # and analyzers each get their own handle(ev) call off the same
        # queue, see module docstring's projection-math section) --
        # deliberately mirrors that analyzer's exact "clock_tick while not
        # running is ignored" gate.
        self._running = False
        self._now = float(now) if now is not None else time.time()
        # Paper-grid beat-zero anchor (module docstring's "Adapted, not a
        # literal port" section) -- defaults to construction time so the
        # grid is already "scrolling" at `idle_bpm` before any transport
        # "start" ever arrives, matching v1's own `last_run_bpm` defaulting
        # to `idle_scroll_bpm` pre-run rather than being undefined/frozen.
        self._beat_zero_ts = self._now
        # `pitch_guide_ys` memoization (review fix): invariant per (pitch_lo,
        # pitch_hi) -- nothing in this class mutates that pair today
        # (pitch-window panning is disclosed future work, module docstring's
        # "Not ported" section), so every `view_model()` call was
        # reformatting the same N note-name strings from scratch. Keyed on
        # the CURRENT pair rather than built once-and-frozen so a future
        # range-change feature invalidates it automatically instead of
        # needing its own cache-busting code -- see `_pitch_guide_ys()`.
        self._pitch_guide_cache_key: tuple[int, int] | None = None
        self._pitch_guide_cache: list[dict] | None = None
        # Active-row tint + fade (Phase 8 Task 4, module docstring's own
        # section) -- pitch -> the real timestamp its tint should fade back
        # to background by, refreshed every `tick(now)` for every pitch
        # currently visible in the window (`_refresh_row_fade()`), mirroring
        # v1's own `self._roll_row_fade_until` dict (owned by the RENDERER
        # in v1, owned by this engine-side state here -- see module
        # docstring for why).
        self._row_fade_until: dict[int, float] = {}

    # -- event handling ------------------------------------------------------

    def handle(self, ev: MidiEvent) -> bool:
        self._now = max(self._now, ev.ts)
        if ev.type == "start":
            self._running = True
            # Re-anchor bar/beat phase to THIS downbeat -- v1's own tick
            # counter resets to 0 on "start" (see module docstring).
            self._beat_zero_ts = ev.ts
            return self._reset()
        if ev.type == "stop":
            self._running = False
            return self._close_all(ev.ts)
        if ev.type == "continue":
            self._running = True
            return False   # true no-op: no v1 "continue" branch touches note/tempo state
        if ev.type == "clock_tick":
            return self._advance_clock(ev)
        if ev.type == "note_on":
            if ev.channel is None:
                return False
            ch = ev.channel + 1
            if ev.data2 == 0:
                return self._note_off(ch, ev.data1, ev.ts)
            return self._note_on(ch, ev.data1, ev.data2, ev.ts)
        if ev.type == "note_off":
            if ev.channel is None:
                return False
            return self._note_off(ev.channel + 1, ev.data1, ev.ts)
        if ev.type == "control_change" and ev.data1 == _ALL_NOTES_OFF_CONTROL:
            if ev.channel is None:
                return False
            return self._clear_channel(ev.channel + 1, ev.ts)
        return False

    def _note_on(self, ch: int, pitch: int, vel: int, ts: float) -> bool:
        key = (ch, pitch)
        prev = self._active.pop(key, None)
        if prev is not None:
            # Retrigger (no intervening note-off) closes the previous span
            # first -- matches v1's `_close_active` call before opening the
            # new one in `on_midi_event`'s note_on branch exactly.
            prev.release_ts = ts
            self._closed.append(prev)
        self._active[key] = _Span(ch=ch, pitch=pitch, vel=vel, onset_ts=ts)
        return True

    def _note_off(self, ch: int, pitch: int, ts: float) -> bool:
        span = self._active.pop((ch, pitch), None)
        if span is None:
            return False   # stray/already-off note-off -- true no-op, mirrors v1
        span.release_ts = ts
        self._closed.append(span)
        return True

    def _clear_channel(self, ch: int, ts: float) -> bool:
        keys = [k for k in self._active if k[0] == ch]
        if not keys:
            return False   # already clear -- true no-op
        for key in keys:
            span = self._active.pop(key)
            span.release_ts = ts
            self._closed.append(span)
        return True

    def _close_all(self, ts: float) -> bool:
        if not self._active:
            return False
        for key in list(self._active):
            span = self._active.pop(key)
            span.release_ts = ts
            self._closed.append(span)
        return True

    def _reset(self) -> bool:
        """v1's `_reset_transport_history()`: a new transport run starts
        from a clean slate -- clears active notes AND history, AND (this
        port's own addition, see module docstring) the derived bpm, mirroring
        `analyzers/transport.py`'s own "start" reset. Always dirty, matching
        that analyzer's "a restart is a real content change" precedent."""
        self._active.clear()
        self._closed.clear()
        self._bpm = None
        return True

    def _advance_clock(self, ev: MidiEvent) -> bool:
        if not self._running:
            return False   # mirrors analyzers/transport.py's identical gate
        if ev.clock_batch_start is None:
            return False   # no prior boundary yet -- nothing derivable
        new_bpm = 60.0 / (ev.ts - ev.clock_batch_start)
        if self._bpm is not None and new_bpm == self._bpm:
            return False
        self._bpm = new_bpm
        # A bpm change only visibly affects the roll in "tempo" mode (see
        # module docstring's projection-math section) -- "wallclock" mode's
        # geometry is bpm-independent, so skip the snapshot churn there.
        # (The VM's cosmetic `window.span_beats` read-out can lag by up to
        # one tick_hz period in that case -- an accepted, imperceptible
        # trade-off, same class as pages/harmony.py's disclosed "harmonic_
        # rhythm's fixed-120bpm" minor gap.)
        return self._projection == "tempo"

    # -- wall-clock scroll-progress hook (see class docstring) ---------------

    def tick(self, now: float) -> bool:
        self._now = float(now)
        self._prune()
        self._refresh_row_fade()
        return bool(self._active) or bool(self._closed)

    def _prune(self) -> None:
        cutoff = self._now - HISTORY_RETENTION_S
        while self._closed and self._closed[0].release_ts < cutoff:
            self._closed.popleft()

    def _refresh_row_fade(self) -> None:
        """Active-row tint + fade (module docstring's own section): refresh
        `_row_fade_until` for every pitch CURRENTLY visible in the window
        (mirrors v1's own per-frame `fade_until = now_mono + _ROLL_ACTIVE_
        ROW_FADE_S` refresh), and drop any entry that has both expired AND
        is no longer visible (an unbounded dict would otherwise grow by one
        entry per distinct pitch ever touched across a long session)."""
        visible = {int(s.pitch) for s, _end_ts in self._visible_spans()}
        for pitch in visible:
            self._row_fade_until[pitch] = self._now + _ROLL_ROW_FADE_S
        expired = [p for p, until in self._row_fade_until.items()
                   if p not in visible and until <= self._now]
        for p in expired:
            del self._row_fade_until[p]

    # -- live controls (engine/core.py's pianoroll.* actions call these) -----

    def zoom_by(self, delta: float) -> float:
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom + float(delta)))
        return self._zoom

    def set_zoom_level(self, level: float) -> float:
        """Set the zoom to EXACTLY `level` (clamped to `[ZOOM_MIN,
        ZOOM_MAX]`, same clamp `zoom_by` enforces) -- unlike `zoom_by`,
        never reads or adds to the CURRENT zoom, so repeated calls with the
        same `level` are idempotent and a lower `level` after a higher one
        jumps straight there. Added as the ABSOLUTE counterpart to
        `zoom_by`'s cumulative delta specifically for `engine/bindings.py`'s
        continuous MIDI-binding mode (see that module's own docstring,
        "Trigger vs continuous" section, for the live-reproduced saturation
        bug a delta-shaped action causes there -- a CC sweep through
        `zoom_by` keeps ADDING on top of whatever the previous message
        already added, pinning to `ZOOM_MAX` almost immediately regardless
        of the knob's actual physical position)."""
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, float(level)))
        return self._zoom

    def set_projection(self, mode: str) -> str:
        mode = str(mode).strip().lower()
        if mode not in _PROJECTION_MODES:
            raise ValueError(f"unknown pianoroll projection mode: {mode!r}")
        self._projection = mode
        return self._projection

    def set_channels(self, spec: str) -> list[int]:
        self._visible_channels = _parse_channel_spec(spec)
        return sorted(self._visible_channels)

    def toggle_channel(self, channel: int) -> list[int]:
        """Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md's keymap
        revamp, docs/visual-audit.md's pianoroll channel-visibility row):
        v1's `d` key toggled channel 10 (drums) specifically, IN/OUT of
        whatever else was visible, unlike `set_channels()`'s own
        replace-the-whole-set semantics (which `*` -- "show all" -- still
        uses, ported as `set_channels("")`). A single-keypress binding has
        no way to supply a full comma/range spec, so this is a genuinely
        new, minimal action: flip ONE channel's membership in `_visible_
        channels`, leaving every other channel's current visibility
        untouched -- exactly v1's own toggle semantics, just reachable via
        the args-table keymap mechanism (`{action: "pianoroll.
        channel_toggle", args: {channel: 10}}`) instead of a hardcoded key
        branch."""
        channel = int(channel)
        if channel in self._visible_channels:
            self._visible_channels.discard(channel)
        else:
            self._visible_channels.add(channel)
        return sorted(self._visible_channels)

    def toggle_projection(self) -> str:
        """Phase 8 Task 6: v1's `p` key TOGGLED between beat/tempo-relative
        projection (`set_projection()` above is the ABSOLUTE setter this
        already exposed -- a fixed keymap.toml args table can only ever
        bind ONE static target mode, which can't reproduce a toggle). This
        is the argless flip: read the CURRENT mode, set the other one --
        `_PROJECTION_MODES` is always exactly the 2-tuple `("wallclock",
        "tempo")`, so "the other one" is unambiguous."""
        other = next(m for m in _PROJECTION_MODES if m != self._projection)
        return self.set_projection(other)

    # -- projection / view model ----------------------------------------------

    def _current_bpm(self) -> float:
        return self._bpm if self._bpm is not None else self._idle_bpm

    def _effective_span_s(self) -> float:
        return self._span_s / self._zoom

    def _effective_span_beats(self) -> float:
        return self._span_beats / self._zoom

    def _span(self) -> float:
        """The ACTIVE projection's own window width, in ITS unit (seconds
        for wallclock, beats for tempo) -- what `_dist()`'s output is
        compared against."""
        if self._projection == "tempo":
            return self._effective_span_beats()
        return self._effective_span_s()

    def _dist(self, ts: float) -> float:
        """Raw (unclamped) distance-back-from-`now` for `ts`, in the active
        projection's own unit. See module docstring's "Tempo-relative
        projection math" section for the full derivation -- this one
        function is the entire port of v1's `TempoTimeline.project_tick`."""
        elapsed_s = max(0.0, self._now - ts)
        if self._projection == "tempo":
            return elapsed_s * self._current_bpm() / 60.0
        return elapsed_s

    def _x(self, ts: float, span: float) -> float:
        if span <= 0:
            return 1.0
        x = 1.0 - self._dist(ts) / span
        return max(0.0, min(1.0, x))

    def _y(self, pitch: int) -> float:
        rng = self._pitch_hi - self._pitch_lo
        if rng <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._pitch_hi - pitch) / rng))

    def _window(self) -> dict:
        bpm = self._current_bpm()
        if self._projection == "tempo":
            span_beats = self._effective_span_beats()
            span_s = span_beats * 60.0 / bpm if bpm > 0 else 0.0
        else:
            span_s = self._effective_span_s()
            span_beats = span_s * bpm / 60.0
        # Phase 10 Task A (docs/demo-feedback-2026-08-12.md items 3+9):
        # client-side extrapolation params -- "origin_ts"/"velocity" let a
        # client advance the WHOLE paper (every x-coordinate this VM
        # carries: notes' x0/x1, grid.beat_xs/bar_xs) locally between
        # snapshots instead of only redrawing on receipt of a new one (see
        # module docstring's "Client-side extrapolation" section for the
        # full derivation and clients/chrome.py::extrapolate_pianoroll_vm
        # for the shared client-side consumer). "velocity" is deliberately
        # ONE scalar, not a per-mode formula the client has to know about:
        # `-1.0/span_s` is algebraically identical to wallclock's own
        # `dx/dt = -1/span_s` AND tempo's own `dx/dt = -(bpm/60)/span_beats`
        # (substitute tempo's `span_s = span_beats*60/bpm` above and they
        # cancel to the exact same expression) -- `span_s` is ALREADY
        # correctly mode-aware by this point, so one formula covers both
        # modes with no branch a client would otherwise have to duplicate.
        # "running" mirrors `_grid()`'s own field of the same name (Phase
        # 10 Task A: co-located here so a client's extrapolation code reads
        # one dict instead of two) -- informational only, disclosed: v2's
        # `_dist()` is wall-clock-driven regardless of transport state (no
        # v1-style "freeze when stopped" -- `_current_bpm()`'s idle-bpm
        # fallback keeps the paper moving even with no clock running, the
        # SAME as v1's own idle-scroll precedent), so `running` does not
        # gate whether `velocity` is valid to extrapolate with; it exists
        # for a client that wants to visually distinguish "live tempo" from
        # "idle fallback" motion, not required for correctness.
        origin_ts = self._now
        velocity = -1.0 / span_s if span_s > 0 else 0.0
        return {"mode": self._projection, "span_s": span_s, "span_beats": span_beats,
                "zoom": self._zoom, "origin_ts": origin_ts, "velocity": velocity,
                "running": self._running}

    # -- paper grid (module docstring's "Paper grid" section) -----------------

    def _grid_instants(self):
        """Yield `(m, x)` for every beat boundary currently inside the
        visible window, closest-to-`now` first. `m` is the SIGNED integer
        beat offset from `_beat_zero_ts` (`m == 0` is the anchor itself,
        always a bar boundary); `x` is `_x()`'s own projection of that
        boundary's real timestamp, so it lands exactly where a note onset
        at the same instant would. Stops once `_dist()` exceeds `_span()`
        -- the same window every note is already filtered against."""
        bpm = self._current_bpm()
        if bpm <= 0:
            return
        period = 60.0 / bpm
        span = self._span()
        if span <= 0:
            return
        m = math.floor((self._now - self._beat_zero_ts) / period)
        emitted = 0
        while emitted < _GRID_MAX_INSTANTS:
            ts = self._beat_zero_ts + m * period
            if self._dist(ts) > span:
                return
            yield m, self._x(ts, span)
            m -= 1
            emitted += 1

    def _grid(self) -> dict:
        beat_xs: list[float] = []
        bar_xs: list[float] = []
        for m, x in self._grid_instants():
            (bar_xs if m % BEATS_PER_BAR == 0 else beat_xs).append(x)
        return {
            "beat_xs": beat_xs,
            "bar_xs": bar_xs,
            "pitch_guide_ys": self._pitch_guide_ys(),
            "running": self._running,
        }

    def _pitch_guide_ys(self) -> list[dict]:
        """Memoized `pitch_guide_ys` (review fix, see `__init__`'s own
        comment) -- rebuilds only when `(pitch_lo, pitch_hi)` differs from
        the cached key, so the common case (every `view_model()` call
        during an ordinary session, where the range never changes) reuses
        the SAME list object rather than reformatting N note-name strings
        every time. Returned BY REFERENCE, not copied -- consistent with
        this VM's existing convention of never defensively copying returned
        data (e.g. `clients/fb/lum.py`'s `RAMPS`/`TIERS`); callers only ever
        read this list, never mutate it.
        """
        key = (self._pitch_lo, self._pitch_hi)
        if self._pitch_guide_cache_key != key:
            self._pitch_guide_cache = [
                {
                    "y": self._y(pitch),
                    "is_c": pitch % 12 == 0,
                    "pitch": pitch,
                    "name": f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}",
                }
                for pitch in range(self._pitch_hi, self._pitch_lo - 1, -1)
            ]
            self._pitch_guide_cache_key = key
        return self._pitch_guide_cache

    def _iter_spans(self):
        yield from self._closed
        yield from self._active.values()

    def _visible_spans(self):
        """Yield `(span, end_ts)` for every span "currently visible in the
        window" -- channel filter, pitch-range filter, and window-membership
        (not yet scrolled entirely past the left edge). This is v1's own
        `highlight_pitches` condition (module docstring's "Active-row tint
        + 1s fade-out" section) AND the exact filter `view_model()`'s
        `notes[]` is already built from -- factored out once so
        `_refresh_row_fade()` (tick-time) and `view_model()` (read-time)
        can never drift from each other's idea of "visible."""
        span = self._span()
        for s in self._iter_spans():
            if s.ch not in self._visible_channels:
                continue
            if s.pitch < self._pitch_lo or s.pitch > self._pitch_hi:
                continue
            end_ts = s.release_ts if s.release_ts is not None else self._now
            if span > 0 and self._dist(end_ts) > span:
                continue   # scrolled entirely past the window's left edge
            yield s, end_ts

    def _row_tint(self, visible_pitches: set[int]) -> list[dict]:
        """Active-row tint + fade VM data (module docstring's own section):
        one `{"y", "intensity"}` per pitch still tinted -- `intensity=1.0`
        while `pitch` is in `visible_pitches` (freshly refreshed by
        `_refresh_row_fade()` at the last `tick()`), else the linear
        fade-out fraction `(fade_until - now) / _ROLL_ROW_FADE_S`, dropped
        entirely once that reaches 0 (no need to draw a fully-decayed row)."""
        entries = []
        for pitch, until in self._row_fade_until.items():
            if pitch < self._pitch_lo or pitch > self._pitch_hi:
                continue   # panned out of the visible pitch range entirely
            if pitch in visible_pitches:
                intensity = 1.0
            else:
                remain = until - self._now
                if remain <= 0.0:
                    continue
                intensity = max(0.0, min(1.0, remain / _ROLL_ROW_FADE_S))
            entries.append({"y": self._y(pitch), "intensity": round(intensity, 4)})
        return entries

    def _overlap_flash(self, notes: list[dict]) -> list[dict]:
        """Overlap-flash VM data (module docstring's own section): groups
        `notes` (already-built VM note dicts) by `y` -- float-exact, the
        same "no epsilon needed" convention `pages/pianoroll.py`'s own grid/
        label-invert matching already established, since two notes on the
        SAME pitch always produce the IDENTICAL `_y(pitch)` value -- and
        runs `_overlap_regions_for_row` per group."""
        by_y: dict[float, list[dict]] = {}
        for note in notes:
            by_y.setdefault(note["y"], []).append(note)
        regions = []
        for y, notes_on_row in by_y.items():
            for region in _overlap_regions_for_row(notes_on_row, self._now):
                regions.append({"y": y, **region})
        return regions

    def view_model(self) -> dict:
        span = self._span()
        notes = []
        visible_pitches: set[int] = set()
        for s, end_ts in self._visible_spans():
            visible_pitches.add(int(s.pitch))
            notes.append({
                "ch": s.ch,
                "y": self._y(s.pitch),
                "x0": self._x(s.onset_ts, span),
                "x1": self._x(end_ts, span),
                "vel": max(0.0, min(1.0, s.vel / 127.0)),
                "active": s.release_ts is None,
            })
        notes.sort(key=lambda n: (n["x0"], n["ch"], n["y"]))
        return {
            "title": "PIANOROLL",
            "notes": notes,
            "window": self._window(),
            "range": {"lo": self._pitch_lo, "hi": self._pitch_hi},
            "grid": self._grid(),
            "row_tint": self._row_tint(visible_pitches),
            "overlap_flash": self._overlap_flash(notes),
        }


class PianorollPage:
    name = "pianoroll"

    def __init__(self) -> None:
        self._state = PianorollState()

    def handle(self, ev: MidiEvent) -> bool:
        return self._state.handle(ev)

    def tick(self, now: float) -> bool:
        return self._state.tick(now)

    def view_model(self) -> dict:
        return self._state.view_model()

    # -- action glue (engine/core.py registers pianoroll.* actions onto these) --

    def zoom_by(self, delta: float) -> float:
        return self._state.zoom_by(delta)

    def set_zoom_level(self, level: float) -> float:
        return self._state.set_zoom_level(level)

    def set_projection(self, mode: str) -> str:
        return self._state.set_projection(mode)

    def set_channels(self, spec: str) -> list[int]:
        return self._state.set_channels(spec)

    def toggle_channel(self, channel: int) -> list[int]:
        return self._state.toggle_channel(channel)

    def toggle_projection(self) -> str:
        return self._state.toggle_projection()

    # -- page-declared actions (Phase 4 Task 0, docs/phase4-notes.md) -------
    #
    # These three used to be registered directly in `Engine.__init__`
    # (`_pianoroll_zoom`/`_pianoroll_projection`/`_pianoroll_channels`),
    # each hand-guarded with its own `if "pianoroll" in self.pages:` block
    # and hand-marking `page.pianoroll` dirty itself. `actions()` below is
    # what `Engine.__init__` now discovers generically (one loop over the
    # whole page roster -- the roster IS the guard, no per-page `if` left)
    # -- dirty-marking moved to that loop's own generic wrapper (applies to
    # every page-declared action uniformly, not just these three), and a
    # plain `ValueError` from `set_projection`/`set_channels` below (an
    # invalid mode/channel spec) is translated to the client-facing
    # `ActionError` by that SAME generic wrapper, not by this page --
    # see `Engine._wrap_page_action`'s own docstring. Only the dict-shaping
    # (`{"zoom": ...}` etc, matching the wire format `midicrt describe`/
    # dispatch clients already expect byte-for-byte) is this page's own
    # job, same as it always was.

    def _action_zoom(self, delta: float) -> dict:
        return {"zoom": self.zoom_by(delta)}

    def _action_zoom_level(self, level: float) -> dict:
        return {"zoom": self.set_zoom_level(level)}

    def _action_projection(self, mode: str) -> dict:
        return {"mode": self.set_projection(mode)}

    def _action_channels(self, spec: str) -> dict:
        return {"channels": self.set_channels(spec)}

    def _action_channel_toggle(self, channel: int) -> dict:
        return {"channels": self.toggle_channel(channel)}

    def _action_projection_toggle(self) -> dict:
        return {"mode": self.toggle_projection()}

    def actions(self) -> list[tuple[str, object, str, dict[str, str]]]:
        return [
            ("pianoroll.zoom", self._action_zoom,
             "Adjust the pianoroll's zoom level", {"delta": "float"}),
            # Review finding (Important, engine/bindings.py's own docstring
            # "Trigger vs continuous" section): the ABSOLUTE counterpart to
            # "pianoroll.zoom" above -- sets the zoom directly rather than
            # adding to it, the shape a continuous MIDI binding (a knob/
            # fader whose CC value should map to a parameter's actual
            # position) needs. "pianoroll.zoom" itself stays as-is for
            # trigger-mode/keymap/CLI one-shot nudges.
            ("pianoroll.zoom_level", self._action_zoom_level,
             ("Set the pianoroll's zoom level directly (absolute; see "
              "pianoroll.zoom for the cumulative delta version)"), {"level": "float"}),
            ("pianoroll.projection", self._action_projection,
             "Switch the pianoroll's projection mode (wallclock|tempo)",
             {"mode": "str"}),
            ("pianoroll.channels", self._action_channels,
             ("Set the pianoroll's visible-channel filter (comma/range spec, "
              "empty = all)"), {"spec": "str"}),
            # Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md keymap
            # revamp): argless counterparts specifically so a single
            # keypress (via the keymap's args-table mechanism) can
            # reproduce v1's TOGGLE keys ('d' -> channel 10, 'p' -> mode) --
            # see PianorollState.toggle_channel/.toggle_projection's own
            # docstrings for why the existing absolute setters above can't
            # serve a toggle themselves.
            ("pianoroll.channel_toggle", self._action_channel_toggle,
             "Toggle one channel's visibility (v1's 'd'=ch10 precedent)",
             {"channel": "int"}),
            ("pianoroll.projection_toggle", self._action_projection_toggle,
             "Toggle the pianoroll's projection mode between wallclock and tempo", {}),
        ]
