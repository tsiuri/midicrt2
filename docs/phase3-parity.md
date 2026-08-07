# Phase 3 parity checklist — v1 → v2

Written 2026-08-07 at Phase 3 close (Task 11), HEAD `b34bc57` (675 tests
green, CI run `31187202020` green). This is the sign-off document for
**Phase 7 cutover** (`docs/superpowers/specs/2026-08-06-midicrt2-design.md`
§9) — nothing in v1 should be missing from this table by omission.

**Task 12 update (2026-08-07, same day, HEAD `49601ea`, 881 tests green):**
Task 11's own inventory sweep (Method step 3 below) found six v1 items —
pages 0/2/4/5/7/11 and `plugins/sysex.py` — with **zero** phase-3 coverage
and filed them under the sign-off's DEFERRED section, "queued for the
phase-3 extension task." Task 12 is that extension task: all six are now
**ported**, closing full phase-3 parity. Every row below and the sign-off
section itself have been updated in place (not just appended to) so this
document reflects the CURRENT state, not a stale gap list — see each
affected row for the individual v1→v2 mapping and any disclosed quirks.

## Method

Built from three sources, cross-checked against each other rather than
trusted individually:

1. **v1's own canonical inventory** — `~/codex/midicrt/README.md`'s Pages
   table (IDs 0–17) and Plugins table, confirmed live against the actual
   loader (`midicrt.py::load_pages`/`load_plugins`, both a sorted
   `glob.glob("*.py")` over `pages/`/`plugins/` — anything with a different
   suffix, e.g. `.py.bak`/`.pybak`/no extension, never loads and is dead).
   Nuance: the glob check proves DEATH (wrong suffix → definitely never
   loads) but not the full shape of LIFE — `plugins/polydisplay.py` IS
   matched by `load_plugins()`'s glob (so it does get the standard
   `handle()`/`draw()` plugin treatment too), but v1's own README Plugins
   table separately annotates it "not a plugin": it's ALSO directly
   imported by name (`pages/notes.py:10`, `from plugins import
   polydisplay`) as a shared-state module, confirmed via grep, not
   inferred from the glob alone.
2. **The phase-3 ledger** — `progress.md` and all eleven `task-N-report.md`
   files in `.superpowers/sdd/2026-08-06-midicrt2-phase3-parity/`
   (motherbase), which record what each task actually ported, what it
   explicitly declined to port, and why.
3. **A fresh gap sweep for this task** — cross-referencing (1) against (2)
   turned up v1 pages/plugins that **no phase-3 task report mentions at
   all**: `pages/ccmonitor.py` (ID 4), `pages/ccgraph.py` (ID 5),
   `pages/proglog.py` (ID 7), `pages/sendnotes.py` (ID 2), `pages/help.py`
   (ID 0), `pages/chordkey.py` (ID 11, distinct from the ported
   `pages/notes.py`), and `plugins/sysex.py`. These are real gaps, not
   oversights in this document — they are called out below exactly like any
   other undone item, not silently dropped from the table.

Every row is one of: **ported** (names the v2 module/page/topic), **folded
into** (v1 behavior absorbed into a broader v2 concern, cited), **not
ported — disclosed** (a phase-3 task considered it and explicitly declined,
with reasoning), or **not ported — gap** (no phase-3 task addressed it at
all; found only by this task's inventory sweep).

---

## 1. Pages (v1 `pages/`, IDs per `README.md`)

| ID | v1 page | v2 home | Disposition |
|----|---------|---------|-------------|
| 0 | Help / Keys (`help.py`) | `pages/help.py` (page `help`) | **Ported (v2-appropriate-equivalent)**, Task 12. v1's page is a static, already-drifted keybinding list (its own list disagrees with `README.md`'s own Keybindings section in places) — and v2 still has no keymap/key→action table (Phase 4, unbuilt). Rather than transcribe stale v1 text, `help` renders the engine's LIVE page roster + full action registry (`engine/actions.py::ActionRegistry.describe()`) — the same data `describe` reports over the wire, via a `bind_info()` callback mirroring `pages/configview.py`'s own engine-info wiring. Once Phase 4's keymap lands, this is the natural place to also show it (`describe`'s `"keymap"` field is already reserved for it). |
| 1 | Notes (`notes.py`) | `pages/harmony.py` (page `harmony`) | **Ported**, Task 5. v1's `notes.py` is the primary UI surface for `zharmony.py`'s chord/scale/key/tension data; v2's `harmony` page mirrors its fields (chord 1st–4th candidates, scale candidates, inside/outside, key, tension, harmonic rhythm, motif). |
| 2 | Send Notes (`sendnotes.py`) | `pages/sendnotes.py` (page `sendnotes`) | **Ported**, Task 12. The only v1 page/plugin that sends real MIDI output — needed genuinely new engine-level infrastructure (`engine/midi_out.py::MidiOutput`, v2's first MIDI OUTPUT surface), not just a page. `SendNotesPage` itself stays pure (key-driven state + a `drain_expired(now)` report); `engine/core.py`'s `sendnotes.key` action performs the actual send, mirroring the "analyzer stays pure, engine acts" split `analyzers/stucknotes.py`'s `drain_alerts()` already established. Two real v1 quirks faithfully preserved (not "fixed"): `KEYMAP`'s `,`/`.`/`g`/`h` entries are structurally UNREACHABLE as note triggers (v1's own control-key branches for channel/gate always intercept those four characters first — confirmed by reading `keypress()`'s full branch order); and `drain_expired`'s front-only FIFO expiry check can leave a later, shorter-gated note stuck behind an earlier longer-gated one. No client-side keyboard binding yet (Phase 4, same as every other page's unbound interactive keys) — reachable via `midicrt action sendnotes.key`. |
| 3 | Transport (`transport.py`) | `overlay.status` (Task 3) + `overlay.timesig` secondary chrome row (Task 6) | **Folded into chrome.** v1's dedicated Transport page (BPM/bar/beat, time signature) is not a page in v2 — its content is chrome shown on *every* page instead of one page you switch to. Deliberate synthesis, disclosed in Task 6's report ("v1 only shows this on its Transport page; v2 shows it on every page"). |
| 4 | CC Monitor (`ccmonitor.py`) | `pages/ccmonitor.py` (page `ccmonitor`) | **Ported**, Task 12. `analyzers/ccmonitor.py::CCMonitorAnalyzer`'s per-channel recent-CC window (`deque(maxlen=6)`, appended verbatim — NOT deduplicated by controller number, matching v1) wrapped by this page. Adds a disclosed v2 peak-hold per `(channel, cc)` (v1 has none), matching the `analyzers/voices.py`/`analyzers/spectrum.py` peak-hold precedent. |
| 5 | CC Dashboard (`ccgraph.py`) | `pages/ccdashboard.py` (page `ccdashboard`) | **Ported**, Task 12. Same `CCMonitorAnalyzer` class, a SEPARATE instance from `ccmonitor`'s (`pages/ccmonitor.py`'s own module docstring: no cross-page analyzer-sharing mechanism exists yet), covering the global insertion-ordered last/peak/freshness half (v1's `OrderedDict`, capped at `MAX_ENTRIES=16`, FIFO-evicted on a NEW key only). The task-11 checklist's "depends on `pages/legacy_contract_bridge.py`, a v1 UI-framework shim with no v2 analogue" was a **false blocker** — reading `ccgraph.py` in full for this task shows that import is never actually called anywhere in the file (dead/vestigial), corrected here. Adds `tick(now)`-driven LIVE/stale freshness (transition-only dirty — a disclosed simplification of v1's smooth per-frame age counter, cheaper against real CC traffic volume). |
| 6 | Event Log (`eventlog.py`) | `pages/eventlog.py` (page `eventlog`) | **Ported**, Task 1/3. v2's default page; clock ticks explicitly suppressed (no spam) per Task 3. |
| 7 | Program Changes (`proglog.py`) | `pages/progchanges.py` (page `progchanges`) | **Ported**, Task 12. `pages/progchanges.py` reuses `pages/eventlog.py`'s exact `{title, count, lines}` VM shape (both fb/tui renderers reuse the same tail-slicing convention) — v1's own `scroll_offset` machinery is confirmed dead code in the shipped build (grepped `midicrt.py`'s keypress dispatch: never wired to a key for page 7), so this ports the OBSERVED tail-only behavior rather than the unreachable scroll API. Uses Task 10's `translate()` fix that first populated `data1` with the program number. |
| 8 | Piano Roll (`pianoroll.py`) | `pages/pianoroll.py` (page `pianoroll`) | **Ported**, Task 7. Both v1 projection concepts (`pianoroll.py`'s "beat" mode and `pianoroll_exp.py`'s tempo-relative math — see ID 16) merged into one page with a `pianoroll.projection {mode}` action toggling `"tempo-relative"`/`"wallclock"`. |
| 9 | Audio Spectrum (`audiospectrum.py`) | `pages/spectrum.py` (page `spectrum`) | **Ported**, Task 8, against **real hardware** (USB C-Media audio interface, live PipeWire capture) — not a placeholder. v1's interactive retuning keys (bins/gain/smoothing/floor/ceil/freq-scale/agg-mode/lowcut/HPF-toggle/device-cycle) not ported — no v2 keybinding-table infra yet (same Phase-4 gap as ID 0); only `audio_device`/`spectrum_bins` are config-adjustable. Peak-hold is a disclosed v2 *addition* (v1 has none). |
| 10 | Tuner (`tuner.py`) | `pages/tuner.py` (page `tuner`) | **Ported (math only), inert pending audio wiring**, Task 6, assessed viable Task 8. Pure post-detection math (`freq_to_note`, `tuning_meter`, smoothing/gating) ported; the audio→pitch pipeline is NOT wired (`on_pitch_sample()` has no caller yet). Task 8 confirmed `aubio` installs cleanly on this exact Pi and a working `AudioCapture` now exists, but activating it needs a second consumer tap on that capture — small, scoped, explicitly NOT done. Registered but deliberately excluded from the default page roster (would only ever show "Listening..." until wired). |
| 11 | Chord+Key (`chordkey.py`) | `pages/chordkey.py` (page `chordkey`) | **Ported**, Task 12. Task 11 called this "consolidated-ish" into `harmony`, filed with "underlying analyzer data is available, no new analyzer work needed" — reading v1's `chordkey.py` in full for this task found that only partly true. Three things it shows are genuinely absent from `harmony`'s task-5 VM contract, not just a presentation gap: (1) an independent, cross-all-roots chord scoring (`chordkey.py`'s own local `_chord_candidates`, NOT a call into `zharmony.py`'s detection) — ported as `analyzers/theory.py::chord_candidates_all_roots`; (2) `get_stable_key()`'s full `top`/`ambiguous` picture, which `harmony.py`'s own docstring explicitly says task 5 does not surface — ported as a new, additive `HarmonyAnalyzer.key_detail()` accessor; (3) the roman-numeral harmonic function, also explicitly out of task 5's scope — ported as `analyzers/theory.py::roman_numeral_for_chord` + `ROMAN_DEGREES`, byte-for-byte from `zharmony.py::_roman_for_chord`, including a real, faithfully-preserved v1 quirk: the lowercase gate is `chord_name.startswith("m")`, and the shipped `CHORDS` asset's major-chord entry is literally named `"maj"` — so even a MAJOR chord's numeral renders lower-case (e.g. `"i (T)"`, not `"I (T)"`), confirmed against v1's real source, not a porting mistake. `pages/chordkey.py` wraps its OWN `HarmonyAnalyzer` instance (same "no cross-page analyzer sharing yet" precedent as `ccmonitor`/`ccdashboard`). |
| 12 | Stuck Heatmap (`stuckheat.py`) | — | **Not ported — disclosed**, Task 6. Lifetime pitch-class/note histogram fed by `zstucknotes.get_stuck_stats()` — a historical-stats feature outside the `overlay.alerts` VM contract Task 6 built. Flagged there as a future page opportunity, not lost logic. |
| 13 | Voice Monitor (`voicemon.py`) | `pages/voices.py` (page `voices`) | **Ported**, Task 4. Displays the merge of `zvoicemonitor.py` + `polydisplay.py` (see §2). |
| 14 | Config (`configui.py`) | `pages/configview.py` (page `config`) | **Ported, narrowed by design.** v1's is an *interactive editor* (writes `settings.json`). v2's is a **read-only viewer** of effective config + engine facts (version/uptime/roster) — deliberate, per spec §5 and the never-writes-config rule (§7 of the design doc: a runtime settings-rewrite once clobbered the user's instrument names in v1; v2 structurally cannot repeat that). No capture-section rows yet (Phase 5 unbuilt). |
| 15 | TimeSig Exp (`timesig_exp.py`) | — | **Not ported — disclosed**, Task 6. Feeds `ztimesig_exp.py` (see §2); its own page never built. Both `ztimesig.py` and `ztimesig_exp.py` run live in v1 simultaneously — only the primary (`ztimesig.py`, feeding the main Transport surface) was ported. |
| 16 | Piano Roll Exp (`pianoroll_exp.py`) | `pages/pianoroll.py` (page `pianoroll`, tempo-relative projection mode) | **Partially consolidated**, Task 7. The tempo-relative projection math (`TempoTimeline.project_tick`, the actually-used "flagship" call site per Task 7's hand-derivation) was ported as a *mode* of the single v2 pianoroll page. The session-memory-browser half of `pianoroll_exp.py` (`memory_max_sessions`, session library browsing) is Phase-5 capture/replay machinery — deferred, not ported. |
| 17 | MIDI IMG2TXT (`img2txtviz.py`) | `pages/img2txtviz.py` (page `img2txtviz`) | **Ported**, Task 10, as a real-time MIDI-reactive procedural ASCII generator (v1 never actually loads images despite the name — verified by reading the source; the `imgbank/continue/` dir is orphaned, 960KB, zero code references). Audio-reactive mode (v1's `a` key, cross-page tap into `audiospectrum.py`) **not ported** — no v2 cross-page analyzer-tap mechanism exists for any page pairing yet; v2's version is permanently in the MIDI-only mode v1's own `a`-toggle-off state represents. Grid is a fixed 40×20 (v1's was terminal/block-size-derived, no fixed default to inherit). |

Dead code found during this inventory, confirmed never loaded by v1's
loader (different suffix than `.py`, so excluded by its own `glob.glob`),
listed for completeness — **not parity gaps**, nothing here ever ran:
`pages/eventlog.py.bak`, `pages/notes.pybak`, `pages/pianoroll.py.bak`,
`pages/pianoroll` (no extension, dated Oct 2025 — an orphaned predecessor
of today's `pianoroll.py`).

## 2. Plugins → analyzers / behaviors / chrome (v1 `plugins/`)

| v1 plugin | v2 home | Disposition |
|---|---|---|
| `beat_counter.py` | — | **Confirmed dead in v1 itself** (no-op stub; real BPM authority was `engine/state/tempo_map.py`) — Task 3 finding. Correctly not ported. |
| `beatflash.py` | `analyzers/beatflash.py`, `overlay.beatflash` | **Ported**, Task 9. v1's binary on/off flash reshaped into a continuously-decaying `intensity` (same 0.1s hold); "stronger on bar" is a disclosed v2 addition (v1 flashes identically on every beat). |
| `bootlogo.py.bak` | — | **Intentionally dropped: dead code.** Never loaded (`.bak` suffix excludes it from the plugin glob); its `main()`-executes-at-import shape predates the `draw()`/`handle()` plugin contract every live plugin follows. Task 9 confirmed via source read; a review pass additionally found a stale orphaned `.pyc` in `__pycache__` (leftover from when it *was* live, pre-`.bak`) — does not change the disposition. |
| `loopprogress.py` | `analyzers/loopprogress.py`, `overlay.loopprogress` | **Ported (bar/beat half only)**, Task 9. The 8-cell progress bar ported (`TOTAL_BEATS=32` reproducing v1's `TOTAL_TICKS=768` at v2's beat-only clock granularity). v1's *other* half — scheduler-health + recent-SysEx diagnostic text drawn to the left of the bar — **not ported — disclosed**: no v2 scheduler-health metric exists, and this specific chrome-row sub-feature (a text summary of recent SysEx traffic, distinct from `sysex.py`'s own command-dispatch feature below) was out of Task 9's scope. *(Corrected 2026-08-07, Task 12: `engine/midi_in.py` DOES now surface a `"sysex"` MidiEvent type with a `sysex_data` payload field, per the `sysex.py` row below — the original "no SysEx event type surfaced" reasoning here is stale; the diagnostic-TEXT sub-feature itself remains unbuilt, just for a different reason.)* |
| `meters.py.bak` | — | **Intentionally dropped: dead code.** Never loaded (`.bak` suffix); per-channel velocity+note inline display, superseded/disabled in v1 itself. Found during this task's inventory sweep, not mentioned in any prior task report. |
| `pagecycle.py` | `behaviors/pagecycle.py` | **Ported, re-interpreted**, Task 9 (disclosed, not a literal port — the task brief's own explicit contract). v1: unconditional interval-based rotation through a *curated subset* (`cycle_pages=[1,6,8,9]`), suppressed only by a recent **keypress**. v2: idle-*triggered* (no MIDI activity for `pagecycle_idle_s`) cycling through the **whole roster**, since MIDI activity is the only engine-observable "someone's using this" signal and v2's roster has no page-ID subset to hardcode against. `cycle_pages`/`user_pause` have no v2 analog. |
| `polydisplay.py` | `analyzers/voices.py` | **Merged**, Task 4 (see `zvoicemonitor.py` row — both merge into one analyzer). |
| `polydisplay.py.bak` | — | **Dead code**, never loaded (co-exists with the live `polydisplay.py`; found during this sweep, harmless). |
| `sysex.py` | `engine/sysex.py` (pure parse/reply-build) + `Engine._handle_sysex`/dispatch helpers (engine/core.py) | **Ported**, Task 12. `MidiEvent` gained a `sysex_data` field (raw payload bytes, no F0/F7 framing); `engine/midi_in.py::translate()` populates it. `engine/sysex.py::parse_command`/`build_reply` port `handle()`'s prefix-check + version-negotiation and `_send_reply`'s frame construction byte-for-byte (pure, no side effects — same split as `analyzers/theory.py` vs `analyzers/harmony.py`). `Engine._handle_sysex` is the DISPATCH half: `CMD_SWITCH_PAGE` resolves v1's numeric page IDs through a new `_SYSEX_PAGE_ID_MAP` covering every v2 page this whole phase-3 pass built, dispatched through the SAME `_page_goto` a normal `page.goto` action uses; `CMD_PAGE_CYCLE` dispatches through a new, unconditionally-registered `pagecycle.enable` action (one capability, two entry points — same precedent as `page.goto`); `CMD_SCREENSAVER`'s "wake" needs no direct code at all (any matched-prefix command already bumps engine activity, mirroring v1's own unconditional wake, letting `ScreensaverBehavior`'s existing real-activity-advance restore path fire normally), while "force on" dispatches `page.goto screensaver` directly — inheriting (not introducing) that call's pre-existing "no auto-restore for a manual goto" property, disclosed in the method's own docstring. `CMD_CAPTURE_RECENT` correctly replies error, not fake success — capture is Phase 5, unbuilt. `CMD_CAPABILITIES`'s payload adapts v1's profile/backend bytes (disclosed `0`, no v2 analog — fb/tui are separate client binaries here, not engine-side profiles) and reports the real v1 page-ID vocabulary this build can reach. Replies flow through `engine/midi_out.py::MidiOutput.send_sysex` — the SAME shared output port `pages/sendnotes.py` uses (v1 used two DIFFERENT mechanisms for these two features; a disclosed consolidation, see that module's own docstring). A v2 addition: every matched-prefix command emits a `sysex_command` engine event (real-time visibility for any connected client), replacing v1's file-logging (`sysex.log`/`sysex.d/`), which this task does not port. Tests use real captured `.syx` fixtures copied out of `sysex.d/` on the Pi (`tests/fixtures/sysex_captures/`) — four midicrt-addressed legacy commands plus two non-matching frames proving unrelated Cirklon traffic passes through silently unrecognised. Verified live against the real running daemon: a genuine legacy switch-page-8 frame (byte-identical to a real capture) correctly switched the daemon's current page. |
| `timeclock.py` | `analyzers/transport.py`, `overlay.status` | **Ported**, Task 3. BPM computed exactly (`60/(ts - clock_batch_start)`, no smoothing) rather than v1's smoothed estimate — a disclosed improvement, not a regression. |
| `zharmony.py` (+ top-level `harmony.py` matching engine) | `analyzers/harmony.py`, `analyzers/theory.py` | **Ported**, Task 5. `config/chords.csv`/`config/scales.csv` copied verbatim to `assets/chords.csv`/`assets/scales.csv`, byte-diffed to confirm. Several deliberate simplifications disclosed inline in `analyzers/harmony.py`'s docstring (anti-flicker tension-hold timing, dead legacy-entry filtering). **Not ported — disclosed:** harmonic-rhythm timing uses a fixed `HARMONIC_RHYTHM_BPM = 120.0` (`analyzers/harmony.py:186`) rather than v1's live-bpm read (`getattr(midicrt, "bpm", 120.0)`) — v2's analyzers have no cross-analyzer read access yet (transport's live bpm lives in a sibling analyzer, `engine/core.py`'s own docstring calls this a currently-latent gap, not something Task 5's scope extended to close), so harmonic-rhythm always assumes 4/4-at-120bpm, matching v1's own fallback exactly but never updating from it. Cross-wiring transport bpm into pages is flagged as a small future task. |
| `zscreensaver.py` | `behaviors/screensaver.py`, `pages/screensaver.py` | **Ported, re-architected**, Task 9. v1 writes raw zeros directly into `/dev/fb0`, bypassing the compositor entirely; v2 behaviors act only through actions, so this becomes `page.goto screensaver` (a real blank page) plus manual-override/restore bookkeeping v1 never needed because it never "left" a page. `IDLE_TIMEOUT` (60s) carried over unchanged. Activity filter (`note_on`/`note_off`/`control_change` only, **not** `clock_tick`) reproduced exactly. |
| `zstucknotes.py` | `analyzers/stucknotes.py`, `overlay.alerts` | **Ported (detection only)**, Task 6. WARN_AFTER/CRIT_AFTER age tracking, retrigger-resets-age, CC64 sustain suspension, CC120/123/121 clears all ported. **Not ported (disclosed):** `PANIC_ON_CRIT` (auto all-notes-off MIDI send — real output actuation, excluded by the "no I/O" analyzer rule); `HOLD_AFTER`'s 15s "STUCK CLEARED" message retention (no field in the VM contract); v1's `_fmt_note` octave-offset formatting (raw note numbers used instead, matching `voices.py`'s convention). |
| `ztimesig.py` | `analyzers/timesig.py`, `overlay.timesig` | **Ported**, Task 6, including a from-scratch sub-beat tick reconstruction (v2's clock granularity is coarser than v1's raw 24-ppqn) verified against synthetic click tracks at 3/4, 4/4, 5/4, 7/4. A stop/continue beat-boundary staleness bug found and fixed in the same task's review round. |
| `ztimesig_exp.py` | — | **Not ported — disclosed**, Task 6. Runs live in v1 simultaneously with `ztimesig.py` (both have live `config/settings.json` sections) but feeds only the never-built page 15 (see §1). Real, disclosed gap for a future task. |
| `zvoicemonitor.py` | `analyzers/voices.py` | **Merged with `polydisplay.py`**, Task 4. v1's count-based (not boolean) per-`(channel,note)` retrigger tracking, CC120/123 clear, transport start/stop clear (from `polydisplay.py`'s side, `zvoicemonitor.py` itself has no transport awareness) all ported. **CC64 sustain confirmed absent in both v1 sources** (grepped, zero references) — v2 matches this exactly, not a v2 omission; a named regression test (`test_sustain_cc64_is_not_special_cased`) documents it as deliberate. **Not ported (disclosed):** the POLY-LIMIT-EXCEEDED warning-log feature (`_events` deque comparing against `poly_limit_global`/`poly_limit_ch`) — no VM field requested, no config key added. |

## 3. Cross-cutting chrome (v1's bottom-row stack, all four rows accounted for)

v1 physically reserves four bottom screen rows (bottom-to-top: beatflash →
loopprogress → timeclock → zstucknotes), confirmed by re-reading each
plugin's `Y_POS_OFFSET` in Task 9. v2 reproduces all four as **shared chrome
painted on every page** rather than page-specific drawing, in three rows
(two v1 concerns per row where they're cheap/glanceable, per Task 6's own
"alerts win, falls back to timesig" and Task 9's "beatflash+loopprogress
share a row" precedents):

| Chrome row (v2, bottom to top) | v1 sources | Status |
|---|---|---|
| beatflash glyph + loopprogress bar | `beatflash.py`, `loopprogress.py` | Ported, Task 9 |
| status (BPM/bar/beat/running/source) | `timeclock.py` | Ported, Task 3 |
| alerts (wins) / timesig (fallback) | `zstucknotes.py`, `ztimesig.py` | Ported, Task 6 |

Screensaver correctly suppresses **all three** chrome rows (fixed in Task
9's review pass — the first cut left them lit, a reviewer-caught bug).

## 4. Config domains (`~/codex/midicrt/config/settings.json` → v2 `config.toml`)

| v1 `settings.json` section | v2 status |
|---|---|
| `core` (fps, ipc, module_scheduler, autoconnect, tempo_metrics, feature_flags, runtime_policy) | v1-only engine-internals (its own scheduler/degrade-steps machinery); v2 has a differently-shaped engine with no equivalent knobs. Not applicable to a port. |
| `panic` | Feeds `stuck_notes.panic_*` — not ported (see `zstucknotes.py` row, §2). |
| `capture` | Phase 5, unbuilt. |
| `instruments` | **Ported** — the 16 real names copied into v2 `config.toml`'s `instruments`, Task 4. |
| `pagecycle` | **Ported, reinterpreted** — see §2. `cycle_pages`/`user_pause` not carried. |
| `harmony` | **Ported** — Task 5's `analyzers/harmony.py` defaults mirror this section's tuning constants. |
| `screensaver` | **Ported** — `idle_timeout` → `screensaver_after_s`, 1:1, Task 9. |
| `stuck_notes` | **Ported (detection thresholds only)** — `warn_after`/`crit_after` carried; `panic_*` keys not ported (§2). |
| `timesig` | **Ported** — Task 6. |
| `timesig_exp` | **Not ported** — Task 6 (§2). |
| `voice_monitor` | **Ported** — Task 4. |
| `eventlog` | **Ported** — Task 1/3. |
| `pianoroll` / `pianoroll_exp` | **Ported (merged)** — Task 7 (§1, ID 8/16). |
| `tuner` | **Ported (inert)** — Task 6/8 (§1, ID 10). |

## 5. Scripts (v1 `scripts/` + top-level tools)

| v1 script | Disposition |
|---|---|
| `scripts/midisend.py` + top-level `./midisend` wrapper | **Not ported — out of parity scope.** A manual MIDI/SysEx test-sending CLI tool, not a runtime visualizer feature. v2's `midicrt` CLI (`midicrt action ...`) plus plain `mido` scripts cover the same ad hoc testing need differently; no direct 1:1 needed. |
| `scripts/run_web_observer.py` | **Superseded by a parallel track, not merged.** v1's read-only web dashboard entry point. v2's equivalent, `midicrt-web` (aiohttp-based observer + opt-in control surface), was built end-to-end on an isolated branch (`web-client`, head `7509863`, 221 tests, never touched master or the Pi) **ahead of schedule during Phase 3** but its merge and review are explicitly **deferred to Phase 6 proper** per `progress.md`. Not part of this Phase 3 sign-off. |
| `scripts/aggregate_ci_timings.py`, `scripts/calc_conflict_rate.py`, `scripts/run_parallel_pilot_daily.py`, `scripts/verify_observer_reconnect.py` | **Not ported — out of parity scope.** v1-era CI/process tooling tied to v1's own repo and web-observer testing needs. v2 has its own GitHub Actions CI; these are development-process tools, not application features. |

## 6. Structural / architecture-level items (not single files)

- **MIDI-learn / keymap** (v1 has no equivalent — v2's binding-layer design
  from the spec, §4) — Phase 4, unbuilt, not a v1 parity item.
- **Capture/replay (event sourcing)** — v1's `pianoroll_exp.py` session
  memory browser, `captures/` directory, `deep_research` replay contracts —
  Phase 5, unbuilt.
- **Web client** — see §5 (`run_web_observer.py` row); built on an isolated
  branch, merge deferred to Phase 6.
- **Input layer / key→action table** — v1's global 0–9/`!@#$%^&*` page-switch
  keys and every page-specific keymap (§1's IDs 0, 2, 8, 9, 17 all note
  unported interactive keys) have no v2 equivalent yet; `phase3-notes.md`'s
  known-latent item, explicitly Phase 4 ("config-driven" key table).

## 7. Phase 7 cutover checklist item (carried forward, not resolved here)

- **REAL-REBOOT audio verification is deferred to the Phase 7 cutover
  window** (Task 8's report, both the original landing and its fix round):
  the boot-race fix for `midicrtd.service`'s audio capture
  (`XDG_RUNTIME_DIR=/run/user/1000`, PipeWire-dependency ordering) was
  verified via a simulated PipeWire stop/start cycle, not a real Pi reboot —
  per this project's explicit "do NOT autonomously reboot the appliance"
  rule. **Before cutover, a real reboot must be performed and audio capture
  (`midicrt-fb --out` showing `available: true` with a real device) verified
  post-boot**, not just simulated.

---

## Sign-off — what the user is being asked to accept

**Accept as intentionally DROPPED** (dead code or explicitly out of v2's
design; no future task is planned to revisit these):

- `plugins/bootlogo.py.bak`, `plugins/meters.py.bak`, `plugins/polydisplay.py.bak`,
  `pages/*.bak`/`.pybak`/extensionless — all confirmed dead in v1 itself, never loaded.
- `plugins/beat_counter.py` — confirmed dead no-op stub in v1.
- Page 12 Stuck Heatmap, Page 15 TimeSig Exp (`ztimesig_exp.py`) — historical/experimental features, disclosed as future opportunities not lost logic.
- v1's interactive per-page retuning keys (spectrum's bins/gain/etc., pianoroll's channel-visibility editor, img2txtviz's gamma/ramp keys) — blocked on the Phase 4 keymap table, not silently lost; will return once that infrastructure lands, if desired.
- `zstucknotes.py`'s `PANIC_ON_CRIT` auto-MIDI-panic-send, `zvoicemonitor.py`'s poly-limit-exceeded warning log, `loopprogress.py`'s scheduler-health/SysEx diagnostic text — analyzer purity ("no I/O") and missing VM contract fields, all disclosed at the task that made the call.
- CC64 sustain-pedal handling — v1 never had it either; not a regression.
- v1's config-editing UI (`configui.py`, page 14) — v2's config page is deliberately **read-only**, a permanent design decision (never repeat the v1 settings-clobber incident), not a temporary gap.

**CLOSED by Task 12 (2026-08-07, same day)** — the six items this
document's own inventory sweep found with zero phase-3 coverage, formerly
listed here as DEFERRED, are now **ported**. See §1 (pages 0, 2, 4, 5, 7,
11) and §2 (`plugins/sysex.py`) above for the individual v1→v2 mappings
and disclosed quirks; nothing below remains open for these six.

**Accept as DEFERRED** (real gaps or later-phase work, on an explicit future plan):

- Harmonic rhythm's fixed `HARMONIC_RHYTHM_BPM = 120.0` (Task 5, §2 above)
  — v1's common case reads live bpm; v2 can't yet because analyzers have
  no cross-analyzer read access. Cross-wiring transport bpm into pages is
  a small future task, not scheduled.
- Tuner audio wiring (page 10) — math ported, capture path proven working (Task 8), just not connected; small scoped follow-up.
- Web client (`midicrt-web`) — fully built (221 tests) on an isolated branch, review/merge deferred to Phase 6.
- Capture/replay, MIDI-learn/keymap — Phase 5 and Phase 4 respectively, per the original design spec's phasing; untouched by design, not oversight.
- **REAL-REBOOT audio verification** — must happen at the Phase 7 cutover window (§7 above), not before.
