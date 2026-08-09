# v1 Visual-Feature Audit

Phase 8 Task 1. Pure code-reading exercise (comments included) over v1
(`~/codex/midicrt` on the Pi) to extract every visual/animation/shading/
burn-in feature, cross-checked against v2's actual renderers
(`~/midicrt2/src/midicrt/clients/{fb/app.py,tui.py,chrome.py}`) and against
`docs/phase3-parity.md` (which covers *features*, not visuals). Method:
full read of every listed file (pages incl. `.bak`/no-ext dead variants,
plugins incl. `.bak`, `fb/compositor.py` + `fb/compositor_renderer.py` +
`fb/psf_font.py` + `fb/terminal_capture.py`, `ui/renderers/*`,
`ui/overlays.py` + `ui/composition.py`, `midicrt.py` end-to-end,
`config/settings.json` end-to-end), plus one targeted extension read of
`engine/modules/pianoroll_state.py` (directly implements the pianoroll's
play/pause+BPM scroll coupling the brief calls special attention to — not
in the required list but load-bearing for that one ruling).

**v2 status legend**: PRESENT (byte/behavior-equivalent) / MISSING (no v2
equivalent found) / DIFFERENT (v2 has *something* here but it diverges in
a way worth a build decision — detailed inline, not just labeled).

**Convention**: rows for code confirmed dead in v1 itself (wrong-suffix
files never matched by `midicrt.py`'s `glob.glob("*.py")` loader) are
marked **DEAD-IN-V1** in the v2-status column and excluded from the
summary counts — nothing there ever ran, so v2 owes it nothing. This
matches `phase3-parity.md`'s own convention.

---

## 0. Read-before-anything: two structural discoveries that recolor several rows below

These aren't page-specific; they change how several PRESENT-looking v1
rows should actually be read, so stating them once up front:

1. **v1's screensaver blanking does nothing on the real CRT (compositor)
   render path.** `plugins/zscreensaver.py::_blank_fb()` explicitly
   refuses to write when `midicrt._compositor is not None`
   ("Compositor manages fb0 directly — don't fight it with a raw write").
   `midicrt.py`'s `_ui_loop_body()` early-continue that would otherwise
   skip full-frame rendering and call `_ss.draw(state)` is *also* gated on
   `_compositor is None`. Net effect: in `run_compositor` mode (the
   profile that actually drives the physical CRT), `is_active()` still
   flips true after 60s idle (other plugins read it — `zstucknotes.py`
   suspends its own warnings on it, `sysex.py` wakes it) but **the screen
   never blanks** — the current page keeps rendering, header marquee keeps
   scrolling, badge keeps animating. Screensaver blanking is real only in
   `run_tui`/text mode. v2's screensaver (a real page-swap, not a
   framebuffer side-channel) does not have this bug — worth deciding
   whether v2 should *faithfully reproduce* v1's actual (broken) CRT
   behavior or keep its own (working) one. Flagged inline on the
   screensaver row below.
2. **`fb/terminal_capture.py::TerminalCapture` strips all ANSI styling**
   (its own comment: "colour, bold, etc. are intentionally ignored") when
   compositing legacy `draw()`-path plugin output onto the real CRT. Two
   v1 chrome effects that rely on `term.reverse()` therefore have **no
   visible effect in compositor/CRT mode**, only in `run_tui`/text mode:
   `plugins/timeclock.py`'s beat-synced "TIMER" label inversion, and
   `plugins/zstucknotes.py`'s CRIT-level reverse-video line. Both are
   flagged inline below with the real (CRT-only-plain-text) v1 behavior,
   not the more dramatic text-mode behavior a source read alone would
   suggest.
3. **A second, distinct capture mechanism produces the same "reverse-video
   never reaches the CRT" outcome for PAGE-level (not plugin-level) direct
   writes.** `pages/notes.py::draw()` writes several `term.reverse(...)`
   segments straight to `sys.stdout` (the chord/scale spotlight, the
   inside/outside unique-fraction highlight, the inline CC badge).
   `engine/page_contracts.py::capture_legacy_page_view` (the function
   `notes.py`'s `build_widget()` uses to turn that `draw()` output into a
   widget) only intercepts calls to `draw_line` — swapped into the drawing
   module's globals for the duration of the call, lines 52–58 — it never
   touches `sys.stdout`. Meanwhile `midicrt.py`'s `ui_loop()` redirects the
   REAL `sys.stdout` to `os.devnull` for the whole frame in compositor
   mode. So a direct `sys.stdout.write(term.reverse(...))` inside a page's
   `draw()` (as opposed to a call routed through `draw_line`) isn't merely
   stripped of styling on the real CRT the way finding 2's plugin-chrome
   effects are — it's discarded outright, never even reaching the widget
   as plain text. Flagged on the three affected Notes-page rows in §2.

---

## 1. Page 0 — Help / Keys (`pages/help.py`)

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | interactive keys | v2 status |
|---|---|---|---|---|---|---|
| Static key list | Plain unstyled text list, 19 lines, drawn once per frame at fixed rows starting y=2 | static (no timer) | single brightness (plain green text) | none | none (page has no `keypress()`) | DIFFERENT — v2's `pages/help.py` (fb: reverse-video header + "-- Pages --"/"-- Actions --"/"-- Keymap --" sectioned dump; TUI: same sections, no reverse header) shows LIVE engine data (page roster + full `ActionRegistry.describe()` + keymap), not v1's static hand-written (already-stale) list — a deliberate, disclosed v2-appropriate-equivalent (`phase3-parity.md` ID 0), not a straight visual port. Note v1's own list is already wrong in places (disagrees with its own README) — nothing to preserve pixel-for-pixel here. |

---

## 2. Page 1 — Notes (`pages/notes.py`)

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | interactive keys | v2 status |
|---|---|---|---|---|---|---|
| Per-channel note-name readout | 16 rows `"{ch:02d}  {name:<11}  {notes}"`; `notes` = up to 5 held notes spelled out as `NoteName+Octave(midi###)` via `polydisplay.get_notes()` | per MIDI note_on/off, no clock | plain text, no shading | none | — | **MISSING.** Not a phase3-parity oversight so much as an undersell: parity's ID-13 row says `zvoicemonitor.py`+`polydisplay.py` "merged" into v2's `voices` page, but `voices` only surfaces **counts** (`active/peak` bars per channel, `clients/tui.py::_voices_row_text`) — it never spells out *which* notes are held. v1's literal "what pitches are sounding right now, by name" view has no v2 home anywhere (not `voices`, not `harmony`). Worth a build decision: is this an intentional simplification or a genuine gap to restore? |
| Inline reverse-video CC badge | Appended to each channel's note-text, only while a CC was touched in the last 1s: `term.reverse(" CC:74=100 [2] ")`; blank 16-char filler otherwise (fixed width, "to avoid flicker") | 1s activity window | reverse-video only (no brightness ramp) | none (deliberately constant-width to prevent layout jitter, not a burn-in device) | — | MISSING (no v2 per-channel CC badge; `ccmonitor`/`ccdashboard` cover CC globally, not inline per note-row). **Caveat (§0.3): moot on the real CRT even in v1** — this is a direct `sys.stdout.write`, not a `draw_line` call, so `capture_legacy_page_view` never sees it and it's discarded into `os.devnull` in compositor mode; v1 itself only shows this in `run_tui`/text mode. |
| Chord/Scale "Last/2nd/3rd/4th" history slots | Two label+value row pairs (`_render_slots`), values are last 4 distinct chord/scale labels | on chord/scale label change | plain text; **reverse-video spotlight** on the FIRST slot only, when it's the currently-sounding chord (`chord_rev`)/scale (`scale_rev`) | none | — | **DIFFERENT** — v2's `harmony` page (`_harmony_slot_lines`) ports the 4-slot label+value layout exactly, but the reverse-video "this one is live right now" spotlight highlight is **not reproduced** — every slot renders identically regardless of currentness. **Caveat (§0.3): moot on the real CRT even in v1** — same direct-`sys.stdout.write`-bypasses-capture mechanism as the CC badge above; v1's spotlight is only ever visible in `run_tui`/text mode, never on the deployed CRT. |
| Inside/Outside "unique fraction" row | `_render_row("Inside:", scale_stats, ..., reverse_second=True)` — each of 4 slots shows `total_frac unique_frac`, with only the **unique_frac** portion in reverse video | per note | reverse-video on a text fragment | none | — | **DIFFERENT** — v2's `Inside:`/`Outside:` rows (`_harmony_list_line`) are plain space-joined pitch-class lists, a materially different data shape (v1 tracks 4 historical scale-membership ratios; v2 shows one current inside/outside pitch-class set) as well as losing the reverse emphasis. **Caveat (§0.3): the lost reverse emphasis is moot on the real CRT even in v1** — same mechanism as the two rows above. |
| Chord/Scale confidence + missing tones | Two text lines, `Chord conf: 0.83  missing: F# B` | per note | plain | none | — | PRESENT (`_harmony_conf_missing_line`/`_harmony_conf_missing_text`, same wording, first-candidate-only). |
| Key label + alternates | `Key: C maj (alts: A min 71%)` or `Key: ?→C maj 62%` fallback, ambiguity flagged | per `KEY_UPDATE_EVERY_NOTES` (2 notes) | plain | none | — | PRESENT (`_harmony_key_line`/`_harmony_key_text`). |
| **Tension bar** | 20-char block bar, `"█"×filled + "░"×empty`, `filled = round(score/10*20)`; score 0–10 from pairwise pitch-class dissonance of *currently active* notes | continuous while ≥2 notes held; **holds last value 1.5s** (`_TENSION_HOLD_SECS`) after release before falling back to "silent" | 2-level block-char ramp (no brightness gradient, binary filled/empty per cell) | none (smoothing device, not anti-burn-in) | — | PRESENT — v2 (`_harmony_tension_line`, `_HARMONY_TENSION_BAR_SEGMENTS = 20`, `render_harmony_frame`'s `HARMONY_TENSION_BAR_W` pixel box+fill) matches the 20-segment convention exactly. The 1.5s post-release hold is **not verifiable from client code** — that timing lives in `analyzers/harmony.py` (engine-side, out of this audit's file scope) and would need its own check. |
| Harmonic rhythm line | `Harm.rhy: 1.3 ch/bar  moderate` | derived from chord-change timestamps | plain | none | — | PRESENT, but **DIFFERENT under the hood**: v2 uses a fixed `HARMONIC_RHYTHM_BPM=120.0` instead of v1's live `midicrt.bpm` read (disclosed gap, `phase3-parity.md` §2 `zharmony.py` row) — visually identical when playing at ~120bpm, silently wrong at other tempos. |
| Motif detection line | `Motif:  +4 -2 +7  [x3]` (signed-semitone interval pattern + repeat count) | derived from last 3 melodic intervals | plain | none | — | PRESENT (`_harmony_motif_line`/`_harmony_motif_text`). |

---

## 3. Page 2 — Send Notes (`pages/sendnotes.py`)

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | interactive keys | v2 status |
|---|---|---|---|---|---|---|
| Status line | `Dev: ... Ch:01 Oct:+4 Vel:096 Gate:120ms Active:2` | per keypress / note expiry | plain | none | — | PRESENT (`_sendnotes_status_text`, both clients). |
| Help text rows | 2 static lines listing the QWERTY-row note keymap + adjust keys | static | plain | none | keys: `z s x d c v g b h n j m , l . ; /` = white/black note triggers (QWERTY-row mapped, C4 base); `,`/`.` = channel −/+; `[`/`]` = octave −/+; `-`/`=` = velocity −/+; `g`/`h` = gate −/+ ms | PRESENT as text (`render_sendnotes_frame`/`_lines`); **all listed keys unbound client-side** — v2 has no Phase-4 keymap wiring for this page yet, action is reachable only via `midicrt action sendnotes.key` (per `phase3-parity.md` ID 2). This is exactly the class of thing the keymap revamp needs to restore. |

---

## 4. Page 3 — Transport (`pages/transport.py`)

Not its own page visually in v1 either in the sense of unique drawing — a
thin wrapper: title line + `TransportWidget` (Running/Bar/BPM/Ticks/time
signature), all plain text, no shading, no animation of its own (the
*chrome* rows below carry the actual BPM/beat animation).

v2 status: **Folded into chrome** (`overlay.status` + the timesig half of
the secondary chrome row) — shown on *every* page instead of one you
switch to. Disclosed synthesis, `phase3-parity.md` ID 3. Visually
reasonable — nothing here was animated to begin with.

---

## 5. Page 4 — CC Monitor (`pages/ccmonitor.py`)

Plain text table, 16 rows, up to 6 recent `(cc,val)` pairs per channel,
zero shading/animation. PRESENT — v2's `ccmonitor` page/`analyzers/
ccmonitor.py` reproduces the exact `deque(maxlen=6)` non-deduplicated
window (Task 12, disclosed peak-hold addition not in v1).

## 6. Page 5 — CC Dashboard (`pages/ccgraph.py`)

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | interactive keys | v2 status |
|---|---|---|---|---|---|---|
| Proportional CC bar | `"█"*bar_len` sized to `val/127` against a fixed `bar_region_width`, one row per recently-touched `(ch,cc)`, `OrderedDict` FIFO-capped at 16 | per CC event | single-brightness block fill, no ramp | none | — | PRESENT — v2 `ccdashboard` (`_ccdashboard_bar`/pixel `rect` fill) matches the proportional-bar-plus-age convention. |
| "LIVE" / age-in-seconds freshness label | `"LIVE"` while age<2s else `"12.3s"` | continuous | plain (TUI); reverse-tone-swap on fresh vs stale in fb (`ACCENT_FG` vs `NORMAL_FG`) | none | — | PRESENT, v2 additionally makes freshness **transition-only dirty** (disclosed simplification vs v1's smooth per-frame age counter). |

## 7. Page 6 — Event Log (`pages/eventlog.py`)

Plain scrolling tail of MIDI events, filterable by CC number, with a
scroll-position marker (`⟵ offset N` / `⟵ end of log`) merged into the
last visible line. No shading, no animation beyond the natural scroll of
new lines pushing old ones off. PRESENT — v2's default page, ported Task
1/3 (clock ticks explicitly suppressed to avoid spam, matching v1's own
event-type filtering intent).

## 8. Page 7 — Program Changes (`pages/proglog.py`)

Same shape as eventlog (tailed log + `%`-complete scroll marker), no
shading/animation. PRESENT — v2's `progchanges` reuses eventlog's exact
`{title, count, lines}` VM/renderer shape (Task 12).

---

## 9. Page 8 — Piano Roll (`pages/pianoroll.py`) — the load-bearing page

This is the page the phase-8 decisions doc calls out by name. Three
rendering surfaces exist for it in v1 simultaneously: the shared
`build_roll_view()`/`PianoRollWidget` data model (`pages/pianoroll.py`),
the **text renderer** (`ui/renderers/text/renderer.py`, used in
`run_tui`), and the **fb compositor renderer**
(`fb/compositor_renderer.py::_render_pianoroll`, used in
`run_compositor`, i.e. the real CRT). They diverge significantly — audited
separately below.

### 9a. Shared data model (both renderers read this)

| feature | what it looks like / does | timing-or-sync source | v2 status |
|---|---|---|---|
| Timeline marker row | Text string of `" "`/`":"`(beat)/`"|"`(bar) per column, computed from `tick_right - (roll_cols-1-i)*TICKS_PER_COL` | tick-based | PRESENT as data (`vm["window"]`), but see 9b/9c — neither v2 renderer draws it as a visible ruled line the way the fb compositor's dotted-guide layer does. |
| **"Fixed like paper" scroll** — the core ruling | Notes scroll leftward through fixed pitch rows. **Confirmed exact mechanism** (`engine/modules/pianoroll_state.py::PianoRollState.on_tick`): while `running`, the roll advances on real MIDI clock ticks (`ticks_per_col`-quantized steps of the actual transport tick). While **stopped**, it does **not freeze** — it keeps generating "virtual ticks" from wall-clock elapsed time at `eff_bpm` (the last-known running BPM, falling back to `IDLE_SCROLL_BPM=120` if the transport never ran), i.e. the paper keeps scrolling at a plausible, BPM-locked rate even with no transport clock at all. This is the literal code behind "fixed like paper scrolling across the screen, synced to play/pause and BPM." | MIDI clock tick when running; wall-clock-at-idle-BPM when stopped | **PRESENT (Phase 8 Task 3)** — the note-scroll half of this ruling was already live since Task 7 (`pages/pianoroll.py::PianorollState._current_bpm()`/`_dist()`, driven by `tick(now)` every engine loop iteration regardless of transport state — verified pre-existing, needed no change). Task 3 additionally builds the GRID on the identical dual-clock quantity (`_beat_zero_ts` anchor + `_current_bpm()`-derived period, see that module's own "Paper grid" docstring section) so the grid scrolls in lock-step with notes, including the idle-scroll case (`test_grid_keeps_scrolling_while_stopped_at_idle_bpm`) and the last-run-bpm-persists-through-stop case (`test_grid_uses_last_run_bpm_after_stop_not_idle_bpm`), both in `tests/test_pages_pianoroll.py`. |
| Out-of-range indicator | `header_right`/`footer_right` text (e.g. `"F#7 ch03"`) shown for 2.5s (`OUT_RANGE_HOLD`) when a note is above/below the visible pitch window, with an `_fmt_out_of_range(..., extra=N)` "+N more" suffix | per note-on above/below range, 2.5s hold | **DEAD-IN-V1 for the reverse-video half**: `_draw_right_reverse()` (would show this in reverse video) is only called from `pianoroll.py::draw()` — but `midicrt.py`'s draw loop always prefers `build_widget()` over `draw()` when both exist (`if hasattr(page,"build_widget"): ... elif hasattr(page,"draw"): ...`), and `pianoroll.py` HAS `build_widget()`. So `draw()`/`_draw_right_reverse()` never execute in the shipped app — the "out of range" text exists, but always in **plain text**, never reverse-video, in both v1 render modes today. Confirmed by reading the dispatch order in `midicrt.py`'s `_ui_loop_body`, not assumed. |
| Channel visibility filter | `visible_channels` set, editable via `v`+digit-list input mode; `d` toggles channel 10 (drums) specifically; `*` shows all | keypress | keys: `v` (open filter-input), digits/`,`/`-` (range/list entry), Enter/Esc, `d` (toggle ch10), `*` (show all) | MISSING client-side binding (Phase 4 keymap gap, same class as sendnotes). |
| Pitch window scroll | PgUp/PgDn shift ±12 semitones, Up/Down shift ±1, Home resets to default (36–83) | keypress | keys: `KEY_PGUP`/`KEY_PGDN`/`KEY_UP`/`KEY_DOWN`/`KEY_HOME` | MISSING client-side binding. |
| Style toggle | `y` swaps `PIXEL_STYLE` text↔dense (only meaningfully affects the now-dormant `PixelRenderer`, §13) | keypress | key: `y` | MISSING (moot without the pixel renderer being live anyway). |
| Projection mode toggle | `p` swaps `beat`↔`tempo_relative` (tempo-relative math ported from `pianoroll_exp.py`'s `TempoTimeline`) | keypress | key: `p` | **PRESENT** — v2's single pianoroll page already has a `pianoroll.projection {mode}` action toggling `"tempo-relative"`/`"wallclock"` (Task 7, per `phase3-parity.md` ID 8/16) — just not bound to a client key yet (Phase 4 gap, same as everything else on this list). |

### 9b. Text renderer (`ui/renderers/text/renderer.py`, `run_tui`)

| feature | detail | v2 status |
|---|---|---|
| Velocity → glyph ramp | 4-level: `v≥96→"█"`, `v≥48→"▓"`, `v>0→"▒"`, else `" "` (`TextRenderer._velocity_char`) | **PRESENT, byte-for-byte** — `clients/tui.py::_roll_glyph` uses the identical thresholds/glyphs, explicitly documented in that module's own comment as porting v1's DEFAULT style. |
| Background grid / bar-beat ruling | **None** — the text renderer draws only the timeline-marker text row (`"{'Bars':>7} │" + timeline`) then one row per pitch with note glyphs; no dotted/faint separator lines anywhere in the note area | **MISSING** — v2's TUI pianoroll (`render_pianoroll_lines`) draws a header line + a bare glyph grid with no timeline row, no per-note-name row labels, and no grid markings at all. This matches v1's *text renderer* (which also has no grid — the grid only exists in the **fb compositor** renderer, 9c below), so TUI is honestly at parity with its v1 counterpart; the real gap is 9c. |
| Per-row note-name label | `"{note_name:>7} │"` prefix on every pitch row | MISSING in v2 TUI (dropped deliberately — engine's pitch range is a fixed span independent of terminal height, so a row can span >1 semitone; a misleading label was intentionally omitted, disclosed in `tui.py`'s own comment). |
| Channel color (`PixelRenderer` "dense" mode only, not the default) | See §13 — dormant, off by default in v1 too. | N/A |

### 9c. FB compositor renderer (`fb/compositor_renderer.py::_render_pianoroll`) — the real CRT path

This is where nearly all of the actual "paper grid" visual richness lives,
and where the monochrome mandate bites hardest.

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | v2 status |
|---|---|---|---|---|---|
| **Faint dotted pitch-row grid** ("the paper") | Every pitch row gets a horizontal dotted line at `_ROLL_H_DOT` (RGB 0,38,15 — very dim green), stride 4px (`row_guide_step`), alternating phase per row (`row_idx & 1`); rows on a C (`pitch%12==0`) use a brighter `_ROLL_H_DOT_C` (0,68,27) | static per row, redrawn every frame (not scrolling itself — the dots are screen-fixed; NOTES scroll through them) | 2-level dim-green intensity (C-row vs other) | **Yes — exactly the "paper" ruling.** The dots are the fixed backdrop; notes scroll across/through them. | **PRESENT (Phase 8 Task 3)** — `pages/pianoroll.py`'s `grid.pitch_guide_ys` (one `{y, is_c, pitch, name}` entry per semitone) + `clients/fb/app.py::_draw_pianoroll_grid` (`Surface.dotted_hline`, new primitive) reproduce the exact stride-4/alternating-phase/C-row-brighter look via `clients/fb/lum.py`'s `LUM_FAINT`/`LUM_FAINT_C` (v1's `_ROLL_H_DOT`/`_ROLL_H_DOT_C`, byte-for-byte). Adapted (not byte-identical mechanism): v1 draws via a raw numpy buffer stride, not reproducible under this codebase's "renderers draw only through Surface's methods" convention — `dotted_hline` gives the same visual through ordinary primitives instead. Tests: `tests/test_fb_render.py::test_render_pianoroll_frame_draws_dotted_horizontal_pitch_row_guide`/`..._c_row_guide_is_brighter`. |
| **Dotted bar-boundary vertical guides** | At every bar tick boundary within the visible window, a dotted vertical line (`_ROLL_V_BAR_DOT`, RGB 0,86,33) spans the full roll height, stride 3px (`bar_guide_step`) | tick-based (bar = 24×4 ticks), computed from `tick_left`/`tick_right_edge` derived from the SAME tick anchor the notes use | single dim-green tone | Yes — same "paper" backdrop | **PRESENT (Phase 8 Task 3), amended scope** — `grid.bar_xs`/`grid.beat_xs` (computed by `pages/pianoroll.py::PianorollState._grid_instants()` through the SAME `_x()`/`_dist()`/`_current_bpm()` projection notes use — see that module's "Paper grid" docstring section) + `_draw_pianoroll_grid`'s `Surface.dotted_vline` calls, `LUM_BAR_GUIDE` (v1's `_ROLL_V_BAR_DOT`, byte-for-byte) for bars, `LUM_FAINT` for beats. **Amended beyond v1's own roll-body layer**: task-3-brief.md's own "the job" section asks for BOTH beat and bar dotted verticals in the roll body (v1 only dots BARS there, reserving beat ticks for the separate solid timeline strip below) — this port reuses v1's exact bar-guide stride/geometry for both, distinguished only by brightness tier, a disclosed amendment not a v1 mismatch. Tests: `test_render_pianoroll_frame_draws_dotted_beat_and_bar_verticals`, `test_render_pianoroll_frame_grid_is_drawn_under_notes_not_over_them` (draw-order proof). |
| **Solid bar/beat marker row** (the `"Bars"` timeline strip) | One pixel-row: a solid `GREEN_MID` 1px vertical tick at each bar boundary, `GREEN_DIM` at each beat boundary (not bar) | tick-based | 2-level (mid vs dim green) | contributes to paper-grid feel | **PRESENT (Phase 8 Task 4)** — `clients/fb/app.py::_draw_pianoroll_bars_strip`, a new reserved strip between the header and the pitch-guide roll body (own fixed pixel height, `_pianoroll_bars_strip_height`, since v2's continuous-height layout has no shared `cell_h` row unit to reuse the way v1's ncurses-cell model does). Solid `LUM_MID`/`LUM_DIM` 1px vertical ticks at bar/beat boundaries — needs **no new engine-side data at all**, reuses `pages/pianoroll.py`'s existing `grid.bar_xs`/`grid.beat_xs` (Task 3) verbatim, confirming that module's own "Not ported... needs NO engine-side change" disclosure. Label mirrors v1's exact `f"{'Bars':>7} │"` (`compositor_renderer.py:730`), `LUM_DIM` tone. Both pianoroll fb goldens regenerated (third pass on these fixtures — task-3's own precedent). Tests: `tests/test_fb_render.py` (label presence, solid vs. dotted distinction, ticks confined to the strip's own height, empty-`bar_xs`/`beat_xs` degrades to no ticks). |
| **Per-pitch note-name label column** | Left margin (`LEFT_CHARS` cols) gets `"{note_name:>7} │"` per pitch row (`compositor_renderer.py:857-876`); C rows render `GREEN_BRIGHT`, all others `GREEN_DIM` — same 2-level brightness split as the dotted-grid's C-row emphasis above. **Dynamic invert while the pitch is active**: any pitch currently in `highlight_pitches` (see the active-row-tint row below for exactly what that set contains) gets its label CELL inverted — a `GREEN_MID`-filled rect behind `BG`-colored (black) label text — instead of the static brightness (`compositor_renderer.py:867-869` vs. the `else` branch at 870-875) | per-frame, gated on the same "any note data in the tick window" condition as row-tint | 2-level static brightness (C vs non-C) PLUS a binary invert overlay when active | mild — the invert flips on/off with note activity, a small amount of per-row motion | **PRESENT (Phase 8 Task 3)** — `clients/fb/app.py::_draw_pianoroll_labels`, `PIANOROLL_LABEL_CHARS=9` (the exact `"{name:>7} │"` width), `LUM_BRIGHT`/`LUM_DIM` static split, `LUM_MID`-filled cell + `BG`-colored text for the active-pitch invert. The invert's "any note data visible in the window" condition is reproduced exactly (not simplified to "currently sounding"): `render_pianoroll_frame` builds `active_ys` from `vm["notes"]`'s own already-projected `y` fractions — identical to v1's `highlight_pitches` semantics since v2's `notes[]` is ALREADY filtered to "still visible in the window" the same way. Float-exact row matching (`guide["y"] in active_ys`, no epsilon) verified by `test_grid_pitch_guide_y_matches_note_y_exactly`. Tests: `test_render_pianoroll_frame_label_column_non_c_row_is_dim`/`..._c_row_is_bright`/`..._active_pitch_is_inverted`. **Not reproduced from v1**: the 1s fade-out that accompanies the ROW-body tint (see "Active-row tint + 1s fade-out" below) — the label invert itself is a plain binary on/off in both v1 and this port, no fade on the label cell either way. |
| **Velocity → brightness ramp** | `_velocity_scale(v)`: 0 at v≤0, else linear **50%→100%** brightness (`_VEL_BRIGHTNESS_FLOOR=0.5`) scaled onto the channel's base RGB, precomputed into a 128-entry LUT per channel | per note velocity | continuous linear ramp, floor 50% | none (this is the shading blueprint, not an animation) | **This is the monochrome-conversion blueprint the ruling wants.** v1's own `_CH_BASE_RGB = [(0,255,80)] * 16` — **v1 itself is ALREADY monochrome green for every channel** in the fb compositor; only *brightness* (via this LUT) varies by velocity, not hue. |
| **Per-channel color** | — | — | — | — | **v1 is monochrome here already.** The thing the monochrome mandate needs fixed is entirely on the **v2 side**: `clients/fb/app.py::_ROLL_CHANNEL_PALETTE` is an 8-hue RAINBOW cycle (`(255,60,60)` red, `(255,170,40)` orange, `(255,230,40)` yellow, `(80,230,80)` green, `(60,200,255)` cyan, `(90,110,255)` blue, `(200,90,255)` purple, `(255,90,190)` pink — `(ch-1)%8`), blended from a 25%-dim "charred" floor up to full hue by velocity (`_roll_note_color`). This is precisely the "pianoroll red/orange bars" the decisions doc names. **DIFFERENT — build-priority #1**: replace `_ROLL_CHANNEL_PALETTE`/`_roll_note_color` with a single green base + v1's exact `_VEL_BRIGHTNESS_FLOOR=0.5` linear ramp; channel distinction (if kept at all) should not be hue. |
| **Active-row tint + 1s fade-out** | **Correction from an earlier draft of this audit**: the tint condition is NOT "sounding" — `highlight_pitches` (`compositor_renderer.py:772-793`, comment: "Pitches with any visible note data in the current window") is built from any span/column entry with `velocity>0` whose tick range overlaps the current visible window (`end_tick>=tick_left` / `start_tick<=tick_right_edge`), which includes notes that have already ENDED but are still scrolling across the visible roll. So the whole row gets the background tint (`_ROLL_ACTIVE_ROW_BASE_RGB=(0,38,12)`, 15%-brightness green at full intensity) for as long as ANY note data for that pitch is still on screen, not just while it's being actively held; the fade only starts once the note's span has scrolled entirely OFF the visible window, then fades linearly over `_ROLL_ACTIVE_ROW_FADE_S=1.0`s across 64 quantized steps (`_roll_row_fade_lut`, `compositor_renderer.py:801-825`) | fade timer (`fade_until = now+1.0s`) refreshed every frame the pitch is still in `highlight_pitches`; recomputed from `time.monotonic()` | 64-step linear fade-out, 0%→15%→0% | this IS an animation (continuous per-frame fade) | **PRESENT (Phase 8 Task 4)** — `pages/pianoroll.py::PianorollState._visible_spans()` is the exact `highlight_pitches` condition (already shared with `notes[]`'s own membership filter, task-3's own equivalence finding); `tick(now)` refreshes `_row_fade_until[pitch] = now + _ROLL_ROW_FADE_S` for every visible pitch (v1's own `fade_until` refresh, byte-for-byte `_ROLL_ACTIVE_ROW_FADE_S=1.0`), `view_model()["row_tint"]` reports one `{y, intensity}` per still-tinted pitch. Renderer: `clients/fb/app.py::_draw_pianoroll_row_tint`, drawn UNDER the grid/labels/notes (v1's own layering), colored via `clients/fb/lum.py`'s new `RAMPS["pianoroll_row_tint"]` (`peak=38/255`, proven byte-exact to v1's `(0,38,12)` at intensity 1.0). **Disclosed improvement, not a v1 mismatch**: reports a CONTINUOUS 0..1 fraction instead of v1's own 64-step quantized LUT — smoother fade, same class of change as `analyzers/beatflash.py`'s continuous decay vs. v1's binary flag. Tests: `tests/test_pages_pianoroll.py` (fade math: full intensity while visible, linear decay, re-arm-while-visible, expiry+dict cleanup), `tests/test_fb_render.py` (draw-order: under grid, under notes; intensity-to-brightness scaling; defensive `.get` back-compat). |
| **Overlap flash** | **Correction from an earlier draft of this audit**: when ≥2 notes' pixel-space bars overlap on the same pitch row, the cycle is `total_phases = n+1` (`compositor_renderer.py:980`), not a plain n-way color cycle — phases `0..n-1` show each overlapping note's own color in turn, but phase `n` (the "+1" extra phase) paints plain `BG` (`compositor_renderer.py:993-1000`). So the overlapping region periodically **blinks fully OFF** once per cycle, not just switches color-to-color, at 16Hz × a note-count multiplier (0.70× for <2 concurrently active in that exact pixel — effectively unreachable since flash only starts at 2 — 0.90× for 2, 1.30× for 3, 1.70× for 4+), phase-indexed via `time.monotonic()` (`int(flash_t*flash_hz) % total_phases`) | wall-clock, independent of transport | cycles between full-brightness note colors AND a black/off phase, no dimming | animation — draws attention to overlapping/re-triggered notes, doubles as an anti-static-image device in busy passages | **PRESENT (Phase 8 Task 4)** — `pages/pianoroll.py::_overlap_regions_for_row` ports v1's exact sweep-line algorithm in FRACTION space (v2's `notes[]` already carries continuous `x0`/`x1` through the same projection notes/grid share, so overlap is a property of that data, not a pixel-rounding artifact the way v1's own "drawn pixel intervals" needed it to be) — same `total_phases=n+1` blink-to-BG cycle, same `_FLASH_HZ=16.0` base rate, same count-based multiplier table (0.90×/1.30×/1.70× for 2/3/4+, byte-for-byte). `PianorollState._overlap_flash()` groups `notes[]` by pitch row (`y`, float-exact) and runs the sweep per group, using `self._now` (this class's own single wall-clock domain) in place of v1's `time.monotonic()` — both are arbitrary absolute references only ever used for a modulo phase calc, a disclosed adaptation not a mismatch. Renderer: `clients/fb/app.py::_draw_pianoroll_overlap_flash`, drawn LAST (on top of plain note rects, v1's own "overlap flash pass" order) — a matched note reuses `_roll_note_color` (the SAME velocity-brightness ramp), the BG phase reuses the ordinary background color. **Not needed**: v1's own same-press deduplication step (`seen_keys`) — a v1-pixel-drawing artifact with no v2 equivalent, since `notes[]` already has exactly one entry per span. Tests: `tests/test_pages_pianoroll.py` (pure phase math: `_flash_mult` table, 2/3/4+-note phase cycling incl. the blink-to-BG phase, engage/disengage via note count, cross-pitch-row isolation), `tests/test_fb_render.py` (draw-order: over plain notes; both flash-phase colorings; defensive `.get` back-compat). |
| **1px dark outline per note bar** ("outer ring") | Each drawn note rect gets a 1px `BG`-colored border, so overlapping notes visually cut into each other with a visible dark seam | static per note | n/a (BG-colored) | none (visual clarity device) | MISSING (v2 draws plain filled rects, no inset border). |
| **CC lanes (page 16 memory-mode only)** | Native pixel bars per tracked `(ch,cc)`, height ∝ value/127, brighter fill (`_CC_LANE_BAR_HI`) at v≥96 | per CC event within the visible page window | 2-level brightness threshold | none | See §10 (pianoroll_exp) — this is exclusively a page-16 feature, not drawn on the main page-8 pianoroll. |

### 9d. `PianoRollState` note-span rendering source (feeds both 9b/9c)

Already covered above under "fixed like paper" — restated here only to
flag that BOTH renderers consume the exact same tick-anchored window
(`tick_left`/`tick_right_edge`), so the dual-clock-source scroll behavior
is a property of the shared engine data, not renderer-specific; a
faithful v2 port needs it once, in the data layer, not per-client.

---

## 10. Page 16 — Piano Roll Exp / session memory browser (`pages/pianoroll_exp.py`)

Extends `pianoroll.py`'s base view with a session-memory browser/editor —
this is the "both v1 pianoroll variants" the brief calls out.

| feature | what it looks like / does | timing-or-sync source | v2 status |
|---|---|---|---|
| Mode toggle | `h` arms/disarms recording + switches LIVE ↔ MEM_BROWSER | keypress | key `h`, plus `m`(browser)/`e`(edit)/`p`(playback) mode-select | MISSING (Phase 5 capture/replay, unbuilt — per `phase3-parity.md`, this whole session-memory half is deferred). |
| Session navigation | `,`/`.` step through saved sessions; Left/Right or PgUp/PgDn step pages within a session | keypress | — | MISSING (same Phase 5 gap). |
| CC lane rows (memory mode) | Below the note grid, one row per tracked CC, ASCII intensity ramp `".:-=+*#%@"` (9-level, text renderer) or native pixel bars w/ bright threshold at v≥96 (fb) | per CC event in the page window | text: 9-char ramp; fb: 2-level threshold | none | MISSING (Phase 5). |
| In-place editor overlay | `MEM_EDIT` mode shows Tool/Select/Lane status + an ops-legend row (`q` quantize, `[`/`]` nudge, `t`/`g` transpose, `+`/`-` velocity, `c` cc-scale, `k` cc-thin, `r` program, `o`/`O` split/merge, `y`/`v` copy/paste, `u`/`U` undo/redo, Enter apply, Esc cancel) | keypress-driven | 13 distinct single-key ops | none | MISSING (Phase 5 — "Capture editor: implement (future pass)" per the decisions doc). |
| Playback audition mode | Enter=play/stop, Left/Right=page, `,`/`.`=session | keypress | — | MISSING (Phase 5). |
| Export/import/save | `s` save snapshot, `i` import from library dir, `x` export MIDI | keypress | — | MISSING (Phase 5). |

---

## 11. Page 9 — Audio Spectrum (`pages/audiospectrum.py`)

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | interactive keys | v2 status |
|---|---|---|---|---|---|---|
| Bar-graph spectrum | `"█"` column bars, height ∝ normalized dB level per band (log or linear frequency spacing, configurable), 96 bins default | **correction from an earlier draft of this audit** — the audio callback fires once per `BLOCKSIZE=1024`-sample block at `DEFAULT_SR=44100`Hz, i.e. ≈**43Hz** (44100/1024 ≈ 43.07), not ~30Hz as previously stated → smoothed EMA (`SMOOTHING`) | single-brightness fill, no ramp within a bar | none | **correction from an earlier draft of this audit** — recounting `keypress()`'s actual `if`-branches gives **23** distinct interactive keys, not 20: `[`/`]` bins, `g`/`h` gain, `s`/`a` smoothing, `f`/`v` floor, `c`/`x` ceiling, `j`/`k` display-scale, `z` auto-adapt toggle, `Z` reset scale, `l` lin/log freq, `m` avg/max aggregation, `n`/`N` low-cut, `p` HPF toggle, `,`/`.` device cycle, `0` default device, `r` refresh devices | PRESENT for the bar rendering itself (v2's `spectrum` page, real hardware capture per `phase3-parity.md` ID 9) — but **all 23 retuning keys unbound client-side** (Phase 4 gap; only `audio_device`/`spectrum_bins` are config-file-adjustable in v2 today). |
| Peak-hold tick | — (v1 has **none**) | — | — | — | — | v2 **addition**, not a v1 feature — `-`/hline peak-hold overlay in both renderers, disclosed in `analyzers/spectrum.py`. **Re-verified (Phase 8 Task 4's "spectrum peak-fall if DIFFERENT" checklist item)**: since there is no v1 peak-fall to be DIFFERENT from, this row stays a disclosed v2 addition, not a build item — confirmed the decay is real, not just claimed: `SpectrumAnalyzer.tick(now)` genuinely decays `_peak` toward the live level at `PEAK_DECAY_PER_S=0.7`/sec of INJECTED wall-clock time (never reads a real clock), peak-hold only ever RISES in `on_audio_block()` and only ever FALLS in `tick()` (the standard instant-attack/slow-release peak-meter shape) — already covered by `tests/test_analyzers_spectrum.py::test_tick_decays_peak_hold_toward_the_live_level_over_injected_time`/`test_peak_hold_does_not_fall_just_because_the_level_drops`/`test_tick_never_reads_a_real_clock_only_the_injected_now`, no code change needed. |
| Background thread lifecycle status text | `Input:OK/…  Dev:...  SR:44100  Bins:96 ...` status lines | continuous | plain | none | — | PRESENT equivalent (`_spectrum_header_text`/device info), simplified. |

## 12. Page 10 — Tuner (`pages/tuner.py`)

| feature | what it looks like / does | timing-or-sync source | v2 status |
|---|---|---|---|
| Tuning meter | Text gauge: fixed-width dashes with `\|` at center and `^` at the cents-offset position (`_meter`) | per audio block when signal present | **PRESENT, reused verbatim** — `analyzers/tuner.py::tuning_meter` ported the exact `_meter` math (both clients call the shared function). |
| Note/pitch/cents/confidence/level line | Plain text | per audio block | PRESENT. |
| — | Whole page is **inert pending audio wiring** in v2 (math ported, capture path proven, `on_pitch_sample()` has no caller — `phase3-parity.md` ID 10) — will only ever show "Listening..." until a follow-up wires it. Not a visual gap, a functional one; noted here since it directly affects what's actually visible today. | | |

## 13. Page 11 — Chord+Key (`pages/chordkey.py`)

Plain text: recent pitch-classes, up to 3 ranked chord candidates with
%-match + missing tones, stabilized key (with `=`/`~` tag for
confirmed/ambiguous, `?→label NN%` fallback), roman-numeral function.
No shading, no animation. PRESENT — v2 `chordkey` page (Task 12) ports
all four field groups, including the byte-for-byte roman-numeral
lowercase quirk (`chord_name.startswith("m")` gate) and the `?→` fallback
format.

## 14. Page 12 — Stuck Heatmap (`pages/stuckheat.py`)

Two histogram displays: 12 pitch-class counts (2 rows of 6) and top-5
stuck-note leaderboard (`NoteName(midi###):count`), fed by
`zstucknotes.get_stuck_stats()`'s lifetime counters. Plain text, no
shading/animation. **MISSING** — disclosed, `phase3-parity.md` ID 12
("historical-stats feature outside the `overlay.alerts` VM contract",
flagged as a future page opportunity).

## 15. Page 13 — Voice Monitor (`pages/voicemon.py`)

Two-column, 8-row grid (channels 1–8 | 9–16), each cell
`"{ch:02d} {name:<10} {active}/{limit} pk{peak}"` with a trailing `!` on
warn. Plus up to 3 recent over-limit events with age/flags. Plain text,
no shading. PRESENT — v2 `voices` page (Task 4, merged with
`polydisplay.py`) reproduces active/peak/limit; per-row bar rendering
(`▓`/`░` in TUI, boxed fill+peak-tick in fb) is a v2-native presentation
choice, not a v1 visual to match (v1 has no bar here, just numbers).

## 16. Page 14 — Config (`pages/configui.py`)

Live-navigable JSON tree (arrow/Enter/Left to descend/ascend, `+`/`-`
numeric nudge with accelerating repeat, Space bool toggle, `e` edit
buffer, `s` save, `r` reload), plain text, `>` cursor marker on the
selected row, no shading/animation. PRESENT in spirit, **narrowed by
design**: v2's `config` page (`configview.py`) is a read-only flat dump —
deliberate, per the never-repeat-the-settings-clobber-incident rule
(`phase3-parity.md` ID 14). All of v1's interactive editing keys
(arrows/Enter/`+`/`-`/Space/`e`/`s`/`r`) have **no v2 equivalent by
design**, not by oversight — worth stating explicitly since every other
"MISSING interactive keys" row in this doc is a Phase-4 gap, not this one.

## 17. Page 15 — TimeSig Exp (`pages/timesig_exp.py`)

Plain text: best time-signature label + confidence + event count, pending
label if a change is being confirmed, top-3 candidate list with scores.
No shading/animation. **MISSING** — disclosed, `phase3-parity.md` ID 15
(feeds `ztimesig_exp.py`, which runs live in v1 but its page was never
built; `ztimesig.py`, the primary, IS ported to the timesig chrome row).

## 18. Page 17 — MIDI IMG2TXT (`pages/img2txtviz.py`) — the second load-bearing animation page

Real-time procedural ASCII/pixel generator, MIDI- and (optionally)
audio-reactive. No actual image loading despite the name (verified: no
image file I/O anywhere in the module).

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role? | interactive keys | v2 status |
|---|---|---|---|---|---|---|
| Multi-layer procedural field | Per-cell value = weighted sum of: a scrolling sine wave in X (`wave_x`, driven by `note_phase`+time), a slower sine in Y (`wave_y`, `octave_phase`+time), a radially-expanding "ring" pulse centered near-but-not-exactly screen-center (`ctr_x`/`ctr_y` themselves drift via slow sine), an edge-detect term (`shimmer`, disabled at high perf-degrade), a live audio-spectrum sample per column, and a global pulse term | wall-clock (`time.monotonic()`) for all wave/ring/shimmer phases; audio spectrum sample per-column; note/CC values modulate speed/contrast/edge-mix continuously | maps combined `v∈[0,1]` through `_gamma` (user-adjustable 0.6–2.2), then indexes one of 4 selectable ASCII ramps (10/24/68/6-char density ramps, `_CHARSETS`) | **Yes, explicitly** — this is a genuinely continuous, never-static generative animation; screen content is never the same frame twice even with zero MIDI input, as long as audio or the base wave terms are live | `[`/`]` block-size, `c` cycle charset, `i` invert, `g`/`h` or `-`/`+` gamma, `a` audio on/off, `j`/`k` target-fps cap, `u` auto-quality toggle | **Resolved to DIFFERENT (confirmed) — an earlier draft of this audit left this "not verifiable" pending an engine-side read; now read (`~/midicrt2/src/midicrt/analyzers/img2txtviz.py`, 308 lines).** v2's own module docstring discloses this explicitly as a non-literal port: `_cell_value()` is an independently-specified **3-term** field (two orthogonal sine sweeps + one Manhattan-distance ring sweep, `_TWO_PI`/`_SPEED_X`/`_SPEED_Y`/`_RING_FREQ` constants) versus v1's **~8-term** ad hoc mix. Specifically absent from v2: the **drifting ring center** (v2's ring is Manhattan-distance from a FIXED `(0.5, 0.5)` — `d = abs(nx-0.5)+abs(ny-0.5)` — v1's `ctr_x`/`ctr_y` themselves drift via slow independent sines); the **shimmer/edge-detect term** entirely; **gamma** as a runtime control (`toggle_invert()`/`cycle_charset()` are the only two stateful actions wired — block size/gamma/fps-cap/auto-quality are explicitly documented as "concepts that do not exist in v2's architecture," not ported); and the **trail-decay buffer** (v1's per-cell `_trail` array holding a decaying max — v2 has no equivalent state array at all). v2's `drive`/`brightness_bias` scalars are computed purely from MIDI (`_energy`, `_vel_splash`, CC1 "mod wheel", CC74 "brightness") — there is no live audio-spectrum input anywhere in `analyzers/img2txtviz.py` at all (consistent with — not an additional gap beyond — "Audio reactivity" below, which already covers the SKIPPED-for-now cross-page audio tap). Net: the CONTINUOUS-ANIMATION property survives (v2's `tick()` "ALWAYS returns True... the grid is never truly at rest," by the analyzer's own docstring) but the field's visual complexity is a deliberate, disclosed simplification, not a byte-level port. See the build-priority list's new item on this. |
| Note-triggered "energy"/"spark"/"splash" transients | Non-linear velocity-weighted bursts (`vel_w = vel01^1.85`) added to 3 decaying accumulators (`_energy` exp-decay τ~0.74s, `_spark` τ~0.45s, `_vel_splash` τ~0.125s — "fast transient burst… visible splash on high-velocity hits"), each feeding different visual terms (speed, contrast, ring-rate, direct brightness add) | per note-on, decaying every frame via `_decay()` | modulates brightness/contrast, not a discrete ramp | contributes to the continuous-animation burn-in property above | (same key set) | **PRESENT, confirmed** (resolved from the earlier "not verifiable" flag) — `analyzers/img2txtviz.py`'s own docstring states this triplet is "ported verbatim" from v1: identical `vel01**1.85` weighting, identical `rate_scale` note-density damping, identical per-accumulator clamp ranges (`[0,2.6]`/`[0,2.8]`/`[0,3.2]`) and identical decay-rate constants (`RATE=1.35/2.2/8.0 × RETURN_SPEED_MULT=3.0`, `analyzers/img2txtviz.py:59-67,123-127`). Only the DOWNSTREAM use of these three values differs (feeding the simpler 3-term field above instead of v1's 8-term one), not their own math. |
| Audio reactivity | Cross-page tap into `audiospectrum.py` (peak level + raw RMS envelope), blended into the same drive signal as MIDI energy | live audio | — | contributes to burn-in-safe continuous motion | `a` toggles audio on/off | **SKIP for now per user decision** (2026-08-08 decisions doc: "img2txtviz audio excitation: SKIP for now… cross-page mechanism's resource cost needs its own conversation"). Confirmed `phase3-parity.md` ID 17 also says not ported (no v2 cross-page analyzer-tap mechanism exists yet). |
| Adaptive quality/frame-rate governor | `_quality_boost` (0–3) raises block size under sustained overrun (hysteresis + cooldown), `_target_fps` user-capped 8–60 | measured render time (`_render_ms_ema`) | n/a | n/a (perf, not visual) | `u` toggle auto, `j`/`k` fps cap | Perf-governance concern, not in this audit's visual scope; noted for completeness. |
| Row-diff partial redraw | Only changed rows re-blit each frame (`force_full` vs per-row `!=` check) | n/a | n/a | n/a (perf optimization, incidentally ALSO reduces phosphor "flash" of full-frame redraws, but not designed as a burn-in feature) | — | n/a |

---

## 19. Dead-in-v1 page variants (comments read for intent, never loaded)

| file | what it is | v2 status |
|---|---|---|
| `pages/pianoroll` (no extension, PAGE_ID 8, dated Oct 2025) | **Braille piano roll**: packs 4 semitones into one Braille cell (`DOTS_LEFT`/`DOTS_RIGHT` bit patterns, `BRAILLE_BASE=0x2800`), velocity ≥100 lights the "bright" dot-half; octave boundaries drawn as a solid `"-"*roll_cols` bar instead of the braille row. An earlier, denser predecessor of today's block-glyph `pianoroll.py`. | DEAD-IN-V1 — orphaned predecessor, never loaded (no `.py` suffix). Interesting prior-art for a future "denser roll" mode but not a parity item. |
| `pages/pianoroll.py.bak` | Simpler **block-glyph** predecessor (3-level `█▓░` ramp vs current 3-level `█▓▒`, no config file, no `PianoRollState`/engine module, hand-rolled `deque` scroll buffer) — its own header comment literally says "(no Braille)", confirming it's the braille file's direct successor and today's `pianoroll.py`'s direct ancestor. | DEAD-IN-V1. |
| `pages/notes.pybak` | Minimal ancestor of `notes.py`: just per-channel `polydisplay.get_notes()` printed via `print()`, no harmony/tension section at all. | DEAD-IN-V1. |
| `pages/eventlog.py.bak` | Ancestor of `eventlog.py`: no `build_widget()`, `VISIBLE_ROWS_TARGET=20` (vs live 200), simpler filter-mode key loop. | DEAD-IN-V1. |
| `pages/legacy_contract_bridge.py` | Not a page — a tiny shim (`build_widget_from_legacy_contract`) used by 3 LIVE pages (`audiospectrum.py`, `img2txtviz.py`) to adapt an old-style `draw()` function into a widget via `capture_legacy_page_view`. Live code, not dead, but has no visual content of its own — a plumbing shim. | N/A (no visual to port; `phase3-parity.md`'s `ccgraph.py` row already confirms this import is dead THERE specifically, but it's live via the other two pages). |

---

## 20. Plugins / chrome elements

### 20a. Bottom-chrome stack (the "exact 4-row layout" — confirmed by direct `Y_POS_OFFSET` reads)

Bottom-to-top by `Y_POS_OFFSET`/explicit row math:

| row (bottom→top) | plugin | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role | v2 status |
|---|---|---|---|---|---|---|
| Row 1 (`SCREEN_ROWS-1`, true bottom) | `plugins/beatflash.py` | 2-char block at column 0: `"██"` (reverse-video in text mode) on the beat, `"  "` off; flips on every quarter-note boundary (`tick//24 != last_tick//24`), auto-clears after 0.1s | MIDI clock (24 PPQN), gated on `running` | binary on/off, no ramp | animation, small but constant — a literal metronome pulse | **DIFFERENT** — **correction from an earlier draft of this audit**, which undercounted this as a 4-level ramp and mislabeled its bottom tier: v2's `beatflash_glyph` (`clients/chrome.py`) actually has **five distinct render states**: `_BEATFLASH_LEVELS` (chrome.py:131-136) defines FOUR named glyph tiers — `██` (intensity>1.0, "only a bar flash… ever reaches this" per its own comment), `▓▓` (>0.66), `▒▒` (>0.33), `░░` (>0.0) — PLUS a fifth, separate blank `"  "` fallback returned when the loop falls through with no threshold exceeded (intensity ≤0.0, i.e. fully decayed or before the first beat). The `░░` tier is real and distinct from the blank state, not a duplicate of it as an earlier pass here implied. This reshapes v1's hard binary flash into a decaying-intensity ramp — disclosed v2 addition — and "stronger on bar vs beat" (only a bar-flash reaches the top `██` tier) is also new (v1 flashes identically every beat). **Reconciled (Phase 8 Task 4's "beatflash reconciliation to v1's actual look" checklist item)**: re-read `analyzers/beatflash.py` and `clients/chrome.py` fresh against this row — the analyzer's own module docstring already discloses and justifies both departures (continuous decay instead of v1's per-frame-sampled binary toggle; the bar/beat peak distinction) as deliberate v2 improvements, not gaps to close, and v1's own real quirk (a flash active at the exact instant the transport stops freezes on screen forever, since v1's turn-off check is gated on `running`) is EXPLICITLY NOT reproduced here (this analyzer decays on schedule regardless of transport state, a disclosed strict improvement). No code change was warranted or made — "reconciliation" here means confirming the existing DIFFERENT disclosure is honest and current (it is; the `██`/`▓▓`/`▒▒`/`░░`/blank five-state count above was independently re-verified against the live source, not taken on faith), and folding this mover into the burn-in tripwire (`tests/test_analyzers_beatflash.py`'s existing decay-over-time coverage plus the new cross-mover tripwire suite, see report). |
| Row 2 (`Y_POS_OFFSET=2`) | `plugins/loopprogress.py` | `[   *    ]` 8-cell bar, position = `(tick % (8 bars in ticks)) / total * 8`, only shown (`*` drawn) while `running`; **plus**, to its LEFT, scheduler-health + recent-SysEx-summary text (`sched:ok` / `sched:overload:x,y,z`, and any recent sysex command echo for 5s) | tick-based position; text driven by scheduler diagnostics + `sysex_status`/`sysex_status_time` | plain, single brightness | animation (moving `*`), continuous 8-bar cycle regardless of MIDI content — good ambient anti-static motion | **PARTIAL** — the 8-cell bar itself is PRESENT (`loopprogress_bar`, `LOOPPROGRESS_BAR_WIDTH=8`, same freeze-when-stopped/hidden-`*` behavior). The scheduler-health/SysEx-diagnostic text to its left is **MISSING** (disclosed, no v2 scheduler-health metric exists; `phase3-parity.md` §2). |
| Row 3 (`Y_POS_OFFSET=3`) | `plugins/timeclock.py` | Full-width centered line: `BAR NNNN  BEAT NN  TICK NNN   HH:MM:SS.mmm   TIMER HH:MM:SS.mmm[  fps:N]`; **the "TIMER" label itself inverts (`term.reverse`) on every beat boundary** (`beat_index != last_blink_beat`) while running | beat-boundary blink; wall-clock realtime + a TIMER value that **freezes while stopped but resets to 0.0 on the next start** — **correction from an earlier draft of this audit**, which described this as "pauses/resumes correctly," implying a cumulative session stopwatch. It is not: `timeclock.py:36-47` sets `first_play_after_stop=True` on every stop (line 47), and the NEXT `running` transition sees that flag true and resets `accumulated=0.0` (lines 37-39) before starting to count again — so TIMER is "elapsed time since the most recent play," reset by every stop/start cycle, not a running total across the whole session | binary reverse-video blink on one 5-char label | animation — **but see §0 finding 2: this blink has NO visible effect in compositor/CRT mode** (TerminalCapture strips the reverse-video escape; only visible in `run_tui`/text mode) | PRESENT as text (`overlay.status`/`status_text()` — BPM computed exactly via `60/(ts-batch_start)` rather than v1's smoothed estimate, a disclosed improvement). **The beat-synced blink itself is not reproduced** in either v2 client — worth noting this is arguably reproducing v1's REAL (CRT) behavior rather than a gap, given finding §0.2. The per-run (not cumulative) TIMER reset semantics above are not evaluated for v2 parity here (`overlay.status`'s BPM/BAR/BEAT fields have no TIMER field at all in either client). |
| Row 4 (`Y_POS_OFFSET=4`, topmost of the stack) | `plugins/zstucknotes.py` | `STUCK WARN/CRIT: CH03 F#4(054) 3.2s | ...` (up to `MAX_LIST=3` + "+N more"); **CRIT level additionally reverse-videos the entire line** (separate direct `sys.stdout.write` beyond the normal `draw_line` call); message persists `HOLD_AFTER=15s` after clearing as `"STUCK CLEARED: ..."` | per-note age vs `WARN_AFTER=2.0s`/`CRIT_AFTER=10.0s`; sustain-pedal (CC64) suspends warnings; **correction from an earlier draft of this audit** — CC120 and CC123 both force-clear the channel's stuck-note tracking (`_clear_channel`), but CC121 does NOT: it only resets the `_sustain` flag back to `False` (`zstucknotes.py:164-171`, `elif msg.control == 121: _sustain[ch] = False` — no call to `_clear_channel`). A held-note-past-warn-threshold on that channel keeps counting even after a CC121 (reset-all-controllers) message. | binary reverse-video only at CRIT | mild — an alert, not a designed anti-burn-in device; but see §0.2, **CRIT reverse-video has no visible effect in compositor/CRT mode either** (same TerminalCapture strip) | PRESENT (detection: WARN/CRIT age tracking, sustain suspension, CC clears — `analyzers/stucknotes.py`, `overlay.alerts` — verify the CC120/123-vs-121 distinction above is preserved, not just "CC clears" generically). **MISSING (disclosed)**: `PANIC_ON_CRIT` auto-all-notes-off MIDI send, `HOLD_AFTER` 15s post-clear message retention, octave-letter note formatting. Per the 2026-08-08 decisions, `PANIC_ON_CRIT` moves from dropped→build (configurable, **default OFF** — note v1's own shipped default is `panic_on_crit: true`, so "default off" is a deliberate v2 posture change, not a restoration of v1's default), and `HOLD_AFTER` linger moves from dropped→build. |

Also feeding these same rows: `plugins/timeclock.py`'s `fps_status` text
appended when present, and `plugins/sysex.py`'s command echoes surfacing
via `loopprogress.py`'s left-side text (5s display, `MSG_DISPLAY_SECS`).

### 20b. Header / transport rows (not plugins — `midicrt.py`'s own draw loop)

| feature | what it looks like / does | timing-or-sync source | brightness/ramp | burn-in role | v2 status |
|---|---|---|---|---|---|
| **Page-title scrolling marquee** (row 0) | All loaded pages' titles joined (`"[1:Notes]  [2:Send Notes]  ..."`), scrolled left continuously at `HEADER_SCROLL_SPEED=4.0` chars/sec (config: `core.header_scroll_speed`) whenever the joined string is wider than the screen — doubled-string wraparound (`(page_titles+"    ")*2`) so the scroll loops seamlessly | wall-clock (`time.time()`), independent of MIDI/transport | plain, single brightness | **Yes — a first-class, always-on anti-burn-in device**: constantly-moving text at the very top of the screen, present on EVERY page, all the time, regardless of MIDI activity | **PRESENT (Phase 8 Task 4, fb only)** — `analyzers/marquee.py::MarqueeAnalyzer` ports the exact mechanics: `HEADER_SCROLL_SPEED=4.0` (byte-for-byte), the 4-space gap text (v1's `"    "`), the doubled-string trick, and the engage condition (scroll only when wider than the available width — decided per-renderer in `clients/chrome.py::marquee_window_text(vm, width)`, since fb/TUI have different character budgets, a disclosed adaptation of v1's single-screen-width assumption). `[pid:TITLE]` entries reuse v1's OWN page-ID numbering (`engine/core.py`'s `_SYSEX_PAGE_ID_MAP`, now DERIVED from this module instead of duplicated). Wired into `clients/fb/app.py`: every one of its 15 `render_X_frame` renderers now paints the live marquee slice in its reverse-video header bar instead of a permanently-static per-page title (`_paint_frame` computes the slice once per frame from the real surface width). **TUI NOT wired** (decisions doc ruling #6, "renderer-first: fb/CRT leads; TUI may not support everything") — v1's mechanics are fully ported and testable, just not yet consumed by `clients/tui.py`'s own per-page header text, a disclosed scope cut, not an oversight. Tests: `tests/test_analyzers_marquee.py` (mechanics: offset math, roster ordering, engage condition via width, burn-in tripwire), `tests/test_chrome.py` (`scroll_window`/`marquee_window_text`), `tests/test_fb_render.py` (wiring + the burn-in tripwire rendering the SAME vm at t/t+3s through the real `_paint_frame` path). |
| Transport status row (row 1) | ` RUN/STOP   123.4 BPM   BAR 0042   ●/○` — the `●`/`○` metronome dot flashes based on `tick%24<3` (independent of `beatflash.py`'s own separate flash logic — TWO different metronome indicators exist in v1 simultaneously, chrome row 1's dot and the bottom beatflash block) | MIDI clock, gated on running | binary dot swap | animation (redundant with beatflash but a separate, cheap one) | **DIFFERENT/MISSING** — v2's `status_text()` (`clients/chrome.py`) shows BAR/BEAT/BPM/RUN-STOP/clock-source text but has **no equivalent flashing metronome dot** — v2 relies solely on the beatflash chrome row for that pulse. Not necessarily wrong (avoids the "two indicators for one thing" redundancy) but is a visual reduction worth naming explicitly. |
| Autoconnect-log scrolling message (row 1, right-aligned window) | When `AUTOCONNECT_LOG`'s latest message is too long for its allotted window, it gets its OWN independent scroll offset/timer (same `HEADER_SCROLL_SPEED`), separate from the header's | wall-clock | plain | anti-burn-in (same device as the header, applied to a second text stream) | **DIFFERENT (Phase 8 Task 4, mechanism ported, dormant)** — `analyzers/marquee.py::autoconnect_window_size()` ports v1's exact window-sizing formula (short-message vs. long-message branches) and the SAME doubled-string scroll primitive (`clients/chrome.py::scroll_window()`, shared with the header marquee) is directly reusable for it — but v2 has **no autoconnect-log concept at all client-side** (no MIDI hot-plug log stream exists anywhere in this engine), so there is nothing to feed it. Ported and unit-tested in isolation (`tests/test_analyzers_marquee.py`), disclosed as unwired rather than silently dropped or falsely claimed PRESENT. |
| Row 2 | Always blank (`draw_line(2, "")`) — a deliberate spacer; the real footer/status now lives in the bottom loopprogress-area chrome | n/a | n/a | n/a | n/a (v2 doesn't need to reproduce an intentionally-blank row). |
| Global page-switch keymap | `0`–`9` direct page jump; `!`→11, `@`→12, `#`→13, `$`→14, `%`→15, `^`→16, `&`→17 (shifted-digit mnemonic, i.e. `!` is shift-1 but maps to 11, not 1 — a deliberate non-literal mnemonic scheme); `t`/`T`→10 (Tuner); `C`→trigger MIDI capture-to-file; `q`/`Q`/Esc/Ctrl-C→quit | keypress, global (checked after the current page's own `keypress()` first gets a shot) | — | — | **This is the exact keymap the phase-8 revamp needs to reference/replace.** v2 has its own keymap system already (`engine/keymap.py`, `keymap.toml`, live-reloadable, `describe`-reported) per the Phase 4 work referenced throughout `tui.py`/`fb/app.py` — but the specific v1 shifted-digit scheme above (`!@#$%^&*` → pages 11–17) is the concrete mapping to decide whether to preserve, and is exactly what the decisions doc's "number keys + number+modifier for page jumps; letters for per-page functions" ruling will replace. |

### 20c. Non-visual / data-only plugins (feed pages above, draw nothing of their own)

`plugins/zharmony.py`, `plugins/ztimesig.py`, `plugins/ztimesig_exp.py`,
`plugins/zvoicemonitor.py` — all have `draw(state)` functions but every
one is either a no-op or pure internal state-reset (e.g. `ztimesig.py`'s
`draw()` only resets counters on a start/stop transition); none of them
write a single pixel/character themselves. Their actual screen presence
is entirely mediated through the pages/chrome rows already covered above.
Not separate visual rows in this audit.

### 20d. `plugins/sysex.py` — remote control, not itself visual

Dispatches page-switch/screensaver/pagecycle/capture commands from
inbound SysEx (`F0 7D 6D 63 ...`), logs a status string consumed by the
loopprogress chrome text (§20a). No drawing of its own. PRESENT — v2's
`engine/sysex.py` + `Engine._handle_sysex` (Task 12) ports the full
command set including version negotiation and the capabilities query;
replies route through `engine/midi_out.py`. The file-logging side-channel
(`sysex.log`/`sysex.d/`) is replaced by a `sysex_command` engine event
(v2 addition) rather than ported — not a visual change either way.

### 20e. `plugins/pagecycle.py` — automatic page rotation

Rotates through `CYCLE_PAGES=[1,6,8,9]` every `INTERVAL=300s`, but only
while nobody has pressed a key in the last `USER_PAUSE=3600s` — i.e. it
runs continuously **while the transport plays or not**, gated purely on
recent-keypress, not on MIDI/idle activity. This is itself a mild
anti-static-content device (forces page variety over a long unattended
session). Per the 2026-08-08 decisions doc: **"TURN BACK ON with v1
semantics"** — `phase3-parity.md`'s Task-9 port re-interpreted this as
idle-*activity*-triggered cycling through the *whole* roster instead
(`behaviors/pagecycle.py`), which the parity doc's own fix-wave review
found is **currently unreachable** under stock config (the screensaver's
60s idle threshold always fires first and blocks it, per that doc's
`pagecycle.py` row). **DIFFERENT, explicitly slated for a redo**: v2
needs the literal v1 semantics restored (curated 4-page subset, 300s
interval, rotates while *playing*, suppressed only by a recent keypress
for 3600s, NOT idle-gated) — this is a confirmed, named, already-decided
build item, not an open question.

### 20f. `plugins/zscreensaver.py` — burn-in prevention, the actual mechanism

`IDLE_TIMEOUT=60.0s` of no `note_on`/`note_off`/`control_change` (clock
ticks excluded) → `_active=True` → attempts to zero `/dev/fb0` directly
via its own `mmap`. **See §0.1: this blanking is a no-op whenever
`_compositor is not None`** — i.e. on the actual deployed CRT rendering
path, this plugin's real effect is limited to (a) telling other plugins
"screensaver is active" (suppresses `zstucknotes` warnings) and (b) being
wakeable by any matched SysEx command or keypress. PRESENT + genuinely
**improved** in v2 — `behaviors/screensaver.py`/`pages/screensaver.py`
(Task 9) is architected as a real page-swap (`page.goto screensaver`)
that both clients render as a TRUE full blank with all 3 chrome strips
suppressed too (confirmed in both `fb/app.py::_paint_frame` and
`tui.py::run_tui`'s screensaver branch) — this actually blanks the real
CRT, something v1 itself cannot currently do. `IDLE_TIMEOUT`→
`screensaver_after_s`, 1:1, same 60s default. Worth an explicit call-out:
**v2's screensaver already does the ONE thing v1's CRT deployment cannot
do (actually blank the screen)** — nothing to "port" here so much as a
pre-existing v2 win to keep.

### 20g. Dead-in-v1 plugins (comments read for intent)

| file | what it is | v2 status |
|---|---|---|
| `plugins/beat_counter.py` | Explicit no-op stub, comment says kept only for plugin-load-order stability | DEAD-IN-V1 (confirmed dead even at import time, not just unloaded). |
| `plugins/bootlogo.py.bak` | ASCII "MIDICRT" logo, line-by-line reveal (`time.sleep(0.1)` per line) in green-on-black at startup, `print()`-based (predates the `draw()`/`handle()` plugin contract, `main()` runs at import) | DEAD-IN-V1 — but notable prior art for the separate "Pi boot de-branding" job on the decisions doc's list (a boot-time green ASCII reveal is exactly the "venue-mysterious, no Raspberry Pi branding" aesthetic that job wants — worth a pointer, not a visual-audit action item). |
| `plugins/meters.py.bak` | Right-side inline per-channel velocity bar (`"#"*filled`) + note name/number, appended to each channel row, brightness-independent bar-length-only fade over 1.5s (`fade = max(0, 1.5-age)/1.5`, scales bar LENGTH not color) | DEAD-IN-V1, superseded by `polydisplay.py`'s in-line CC badge approach on the same rows. |
| `plugins/polydisplay.py.bak` | Diagnostic ancestor of live `polydisplay.py`: shows only `"ACTIVE(n)"` per channel, no note names | DEAD-IN-V1 (co-exists harmlessly with the live file under a different name). |

---

## 21. Rendering pipeline internals

### 21a. `fb/psf_font.py` — glyph rendering (no shading of its own)

Loads the system PSF1/PSF2 console font (`/usr/share/consolefonts/
Lat2-VGA8.psf.gz`, 8×8), stamps glyphs as boolean masks into either PIL
RGBA images (cached per `(glyph_idx, fg_color)`) or directly into numpy
RGB/RGB565 buffers via vectorized row-strip masking. **No brightness ramp
or anti-aliasing anywhere** — every glyph pixel is either the exact `fg`
color or fully transparent/background; all shading in the system happens
at the color-selection layer above this (the `_vel_lut`/`_roll_note_color`
etc. covered in §9c), never in the font renderer itself. PRESENT — v2's
`clients/fb/text.py::Font` is an explicit line-by-line port of this exact
loader/stamp logic (same magic-byte detection, same PSF1/PSF2 unicode
table parsing, same RGBA-mask-paste convention), using the same vendored
font asset (md5-verified per that module's own docstring).

### 21b. `ui/renderers/text/renderer.py` — the TUI/text-mode dispatcher (used by BOTH `run_tui` and, until overridden, `run_compositor`'s legacy `draw()` fallback)

Central velocity-ramp definition used everywhere in text mode:
`_velocity_char`: `v≥96→"█"`, `v≥48→"▓"`, `v>0→"▒"`, else `" "` — this is
the ramp §9b (pianoroll TUI) and general note-glyph rendering both draw
from. Also defines `MicrotimingHistogramWidget`'s bar rendering
(`"█"*width + "·"*(16-width)`, a 16-segment proportional bar) and several
other structured-widget flatteners (Transport, EventLog, CaptureStatus,
ModuleHealth, OverlayLayer) — all plain text, no additional shading
conventions beyond `reverse`/`bold` passthrough on styled segments.
PRESENT — this exact 4-level ramp is what v2's `clients/tui.py::_roll_glyph`
explicitly cites and reproduces (§9b).

### 21c. `ui/renderers/pixel.py` — the dormant `PixelRenderer` (SDL2/pygame, off by default)

Only reachable via `--profile run_pixel` **and**
`MIDICRT_ENABLE_PIXEL=1` (falls back to text mode otherwise) — never the
deployed path. Notable because it's the ONE place in v1 where a genuine
**per-channel ANSI/RGB color** convention exists for the pianoroll: in
`"dense"` style mode, `_pixel_char` cycles channel through a 7-color ANSI
palette (`[2,3,4,5,6,7,1]` = green/yellow/blue/magenta/cyan/white/red) via
`term.color(n)`, or (when no color terminal) a 4-level shade cycle
(`█▓▒░` indexed by channel, NOT velocity) — a completely different
shading axis than the fb compositor's velocity-brightness LUT. In the
DEFAULT `"text"` style, it falls back to the same `_velocity_char` ramp as
everywhere else. **Not the source of the monochrome-mandate problem**
(this renderer is dormant, `settings.json` has no `pixel_renderer`
section — meaning it has literally never been activated on this
deployment, confirmed by its absence from the persisted config) — the
real culprit is `clients/fb/app.py`'s own from-scratch `_ROLL_CHANNEL_
PALETTE` (§9c), unrelated to this file. Included here for completeness
per the brief's explicit ask for "every visual-relevant key in
settings.json" (this section's absence IS the relevant finding: dormant
since install).

---

## 22. Config keys (`config/settings.json`, full file read)

Every visual-relevant key not already cited inline above:

| section.key | value | visual effect |
|---|---|---|
| `core.fps` | 60.0 | Target frame rate for `run_compositor` (set explicitly to 60 in `configure_startup_profile` regardless of this value, actually — the config key is present but overridden at profile-select time for compositor mode specifically). |
| `core.header_scroll_speed` | 4.0 | Chars/sec for BOTH the header marquee and the autoconnect-log scroll (§20b) — one knob controls two independent scroll animations. |
| `pianoroll.idle_scroll_bpm` | 120.0 | The wall-clock scroll rate used when transport is stopped and no BPM has ever been observed (§9a "fixed like paper" fallback). |
| `pianoroll.out_range_hold` | 2.5 | Seconds an out-of-range note indicator stays shown after the note stops. |
| `pianoroll.pixel_style` | `"text"` | Selects the (moot, since `PixelRenderer` is dormant) dense/text glyph style — the shipped default is explicitly the plain velocity-ramp style, not the per-channel-color one. |
| `pianoroll.projection_mode` | `"beat"` | Default projection; `p` toggles to `"tempo_relative"` at runtime (not persisted mid-session beyond the in-memory toggle — `_save_cfg()` IS called on toggle, so it does persist). |
| `pianoroll_exp.cc_lane_max_ratio` | 0.25 | Caps CC-lane rows at ≤25% of the page-16 view height. |
| `screensaver.idle_timeout` | 60.0 | §20f — 1:1 ported to v2's `screensaver_after_s`. |
| `stuck_notes.hold_after` | 15.0 | §20a row 4 — v1's actual shipped value for the "STUCK CLEARED" message linger. |
| `stuck_notes.panic_on_crit` | **true** | v1's actual shipped default — note the 2026-08-08 decision to build this in v2 specifies **default OFF**, a deliberate posture change from v1's own default, not a restoration. |
| `stuck_notes.y_pos_offset` | 4 | Confirms the exact chrome-row math used throughout §20a. |
| `pagecycle.enabled`/`cycle_pages`/`interval`/`user_pause` | `true` / `[1,6,8,9]` / `300.0` / `3600.0` | §20e — the exact v1 semantics the decisions doc wants restored verbatim. |
| `tuner.*` (method/tolerance/silence_db/min_conf/smoothing) | yin/0.8/-55.0/0.3/0.55 | Non-visual detection tuning, no rendering effect — listed for completeness since the brief asks for every visual-relevant key and this section's ABSENCE of any visual key confirms tuner has no display-side config at all. |
| *(absent)* `pianoroll_perf` section | not present in the file (code defaults only, `_PIANOROLL_PERF_DEFAULTS` in `compositor_renderer.py`) | Confirms the fb compositor's adaptive perf-tier tracker has never had its defaults overridden on this deployment; the tier thresholds are the as-shipped code defaults. **Correction from an earlier draft of this audit**: the computed tier does NOT actually gate `overlap_flash`/`row_fade`/`dotted_guides` at runtime. `_update_pianoroll_perf_tier()` updates `self._pr_perf_tier` (0–3) every frame based on measured frame time, and `_render_pianoroll` reads it into a local `perf_tier` variable (`compositor_renderer.py:716`) — but that local is never referenced again anywhere in the function. The three effect flags actually used (`compositor_renderer.py:717-722`) come only from the static `effects` config dict, independent of `perf_tier`, and the code's own comment right there explains why: "Keep core piano-roll visuals stable even if adaptive perf tier moves. Tier oscillation can otherwise look like grid/backlight pulsing." So the tier is tracked (presumably for future/diagnostic use) but is a deliberately inert measurement today, not a live effects gate — a v2 port must not invent a tier→effects coupling that doesn't exist in v1. |
| *(absent)* `pixel_renderer` section | not present | Confirms §21c's dormant-since-install finding. |

---

## SUMMARY

Counts exclude DEAD-IN-V1 rows (nothing there ever ran). The adversarial
re-review (see the amendment notes threaded through the sections above)
resolved the one previously-uncertain row (img2txtviz's wave-field
richness, now DIFFERENT-confirmed) and its sibling (energy/spark/splash
transients, now PRESENT-confirmed) via a follow-up read of v2's
engine-side `analyzers/img2txtviz.py` — nothing is left in an unresolved
"not verifiable" state.

| status | count | rows |
|---|---|---|
| **PRESENT** | 32 | Help text (as data-equivalent)†, CC Monitor, CC Dashboard bar+age, Event Log, Program Changes, pianoroll TUI velocity ramp, pianoroll projection-mode toggle (action exists, unbound), Audio Spectrum bars, Tuner meter+readout, Chord+Key (all 4 field groups), Voice Monitor counts, Config (narrowed-by-design, itself a disclosed PRESENT), Notes chord/scale-conf/key/harmonic-rhythm/motif lines (5 sub-rows), Notes tension bar, sysex command dispatch, screensaver (improved), pagecycle mechanism (present but wrong semantics — see DIFFERENT), psf font renderer port, img2txtviz energy/spark/splash transients (confirmed ported verbatim), **pianoroll fb dotted pitch-row grid + C-row brightness** (Phase 8 Task 3), **pianoroll fb dotted beat/bar vertical guides** (Phase 8 Task 3, amended to cover beats too — v1 only dots bars in the roll body), **pianoroll fb per-pitch note-name label column incl. its dynamic active-pitch invert** (Phase 8 Task 3), **header page-title scrolling marquee** (Phase 8 Task 4, fb only — v1's own primary anti-burn-in device), **pianoroll fb solid "Bars" timeline strip** (Phase 8 Task 4, no engine change needed — reuses Task 3's grid data), **pianoroll fb active-row tint + 1s fade-out** (Phase 8 Task 4), **pianoroll fb overlap-flash** (Phase 8 Task 4, n+1 phases incl. blink-to-BG) |
| **DIFFERENT** | 14 | Notes chord/scale reverse-spotlight → lost (moot on real CRT either way, §0.3); Notes inside/outside → reshaped+lost reverse (same §0.3 caveat); Help page → live-data replacement; beatflash → confirmed 5-state ramp instead of binary (not 4-state as an earlier pass here said; re-verified Phase 8 Task 4, no code change warranted); timeclock TIMER blink → moot on real CRT either way; pagecycle → wrong semantics (idle-gated vs interval-while-playing), explicitly slated for redo; transport metronome dot → dropped (beatflash-only now); pianoroll channel color → rainbow hue cycle instead of monochrome brightness (**the** monochrome-mandate item); img2txtviz wave-field → confirmed disclosed simplification (3-term field vs v1's ~8-term; drifting ring center/shimmer/gamma-control/trail-decay all absent); PixelRenderer per-channel ANSI color → dormant, not the deployment path; **autoconnect-log independent scroll** (Phase 8 Task 4 — mechanism/window-sizing formula ported and unit-tested, but no v2 autoconnect-log data source exists to feed it, disclosed dormant) |
| **MISSING** | 19 | Notes per-channel note-NAME list (not just counts); Notes inline CC badge (moot on real CRT even in v1, §0.3); pianoroll fb note-bar outline (static visual-clarity device, not an animation — out of this task's animation/burn-in scope by the audit's own categorization); pianoroll channel-visibility filter keys; pianoroll pitch-window scroll keys; pianoroll style-toggle key; Send Notes keymap (all bound-nothing); pianoroll_exp session-memory browser (whole subsystem, Phase 5); Stuck Heatmap page; TimeSig Exp page; loopprogress scheduler/sysex diagnostic text; zstucknotes PANIC_ON_CRIT + HOLD_AFTER linger (both now slated to build per decisions doc); Audio Spectrum's 23 retuning keys (corrected from 20); pianoroll_exp CC-lane rows; img2txtviz audio-reactivity (explicitly skipped per decision); notes badge / mini-roll-spectrum-piano panel (§ below, deferred — build-priority #6, lower urgency than this task's animation/burn-in items, not named in Task 4's own scope list) |
| **N/A** (chrome/plumbing, no port needed) | 3 | Transport page (folded into chrome by design), row-2 blank spacer, legacy_contract_bridge shim |

*(† "Help text" counted PRESENT because it satisfies the same user need
with better data, per its own DIFFERENT-flavored entry above — border-
line classification, called out so it isn't silently double-counted.)*

**One MISSING item not yet in its own numbered section above** — the fb
compositor's `draw_notes_badge()` (`fb/compositor_renderer.py`, drawn only
while `current_page==1`/Notes): a persistent bottom-right decorative
panel stacking a **mini piano-roll overview** (compressed full-width
timeline + pitch range, with tall "flare" bars when notes go above/below
the visible badge range), a **mini 12-key piano graphic** (white+black
keys, recessed-bevel shading, lit per currently-active pitch class), a
**compact spectrum panel** (falls back to a 48-frame/2s-loop procedurally
animated sine-driven bar pattern — `_build_badge_frames` — when no live
spectrum data exists yet), and a static `"welcome to the jungle ^_^"`
label, all inside a bordered box, refreshed at `_BADGE_UPDATE_HZ=24`.
This is a real, distinctive, always-present-on-the-home-page animated
chrome element with **zero v2 equivalent** in either client (confirmed:
no "badge"/"jungle" hits anywhere in `fb/app.py`/`tui.py`). Counted in
the MISSING total above.

### Build-priority ranking (most visually load-bearing MISSING/DIFFERENT items, judgment call)

1. **Pianoroll fb monochrome conversion** (`_ROLL_CHANNEL_PALETTE` → green
   brightness ramp) — this is THE decisions-doc mandate, on the single
   most-viewed page (pianoroll), on the only render path that reaches the
   real CRT. Do this first; nothing else in this doc blocks it.
2. **Pianoroll fb "paper" grid** (dotted pitch rows + bar-guide verticals
   + timeline strip) — the second explicitly-named decisions-doc item,
   same page, same render path. Natural to build alongside #1 since both
   touch the same `_render_pianoroll` function. **DONE (Phase 8 Task 3)**
   for the dotted pitch-row/beat/bar guides + the per-pitch label column
   (folded into this task's scope, same left-margin region); **the
   separate solid timeline strip is ALSO DONE (Phase 8 Task 4)** — needed
   no engine-side change at all, reused Task 3's own `grid.bar_xs`/
   `beat_xs` data verbatim (`clients/fb/app.py::_draw_pianoroll_bars_strip`).
3. **Header page-title scrolling marquee + autoconnect-log scroll** —
   the decisions doc calls burn-in avoidance "a first-class design
   requirement," and this is v1's single most-visible, always-on,
   every-page anti-burn-in device. Currently v2's headers are static
   per-page bars — the literal opposite of the mitigation. High value,
   likely low effort (pure client-side text animation, no engine changes).
   **DONE (Phase 8 Task 4), fb only** — `analyzers/marquee.py` +
   `clients/chrome.py` + `clients/fb/app.py` (all 15 renderers); TUI
   deliberately not wired (decisions doc ruling #6, fb/CRT leads). The
   autoconnect-log HALF is mechanism-ported-but-dormant (no v2 data
   source exists to feed it) — see its own §20b row for the full disclosure.
4. **Pagecycle semantics fix** — already fully decided (2026-08-08 doc:
   "TURN BACK ON with v1 semantics"), already scoped in `phase3-parity.md`
   (exact config values known: `[1,6,8,9]`/300s/3600s/rotates-while-
   playing). Not a design question, just an implementation debt. **Not
   part of Phase 8 Task 4's scope** (feature/behavior work, not an
   animation/burn-in visual row) — still open.
5. **Pianoroll active-row fade + overlap-flash** — smaller in screen
   footprint than #1/#2 but both are genuine continuous animations (the
   brief's "every animation… is valuable"), and overlap-flash specifically
   solves a real readability problem (stacked/retriggered notes) that a
   static rainbow-color scheme currently papers over differently. **DONE
   (Phase 8 Task 4)** — `pages/pianoroll.py` (`_visible_spans`/
   `_row_fade_until`/`_overlap_regions_for_row`) + `clients/fb/app.py`
   (`_draw_pianoroll_row_tint`/`_draw_pianoroll_overlap_flash`).
6. **Notes-badge (mini-roll/spectrum/piano panel)** — high visual
   distinctiveness (it's the one place v1 shows 3 different data views
   at once, animated, on the app's default/home page) but lower urgency
   than the pianoroll items since it's a secondary decorative element, not
   the primary content of any page.
7. **zstucknotes PANIC_ON_CRIT + HOLD_AFTER** — already decided to build,
   feature work more than visual work (the CRIT reverse-video emphasis
   itself is moot on real CRT per §0.2, so this is about behavior/config,
   not a rendering gap).
8. **Notes per-channel note-name list** — real functional/visual gap
   (§2) worth a deliberate decision (restore vs. accept voices-page
   counts as the replacement) rather than silent loss.
9. **img2txtviz wave-field richness** (drifting ring center, shimmer/
   edge-detect term, trail-decay buffer, gamma as a live control) — added
   on adversarial re-review, confirmed against `analyzers/img2txtviz.py`'s
   own docstring as a disclosed 3-term-vs-~8-term simplification, not a
   verified-absent guess. Ranked below the pianoroll items and the
   burn-in-critical header marquee because img2txtviz is a single
   secondary page (not the primary/default view), but the decisions doc's
   "every animation is valuable" ruling applies here just as much as to
   the pianoroll's overlap-flash/row-fade — the continuous-animation
   *property* already survives in v2 (confirmed: `tick()` never reports
   "at rest"), so this is about restoring visual richness within an
   already-working animation loop, lower risk than building a missing
   mechanism from zero.
