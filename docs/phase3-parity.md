# Phase 3 parity checklist — v1 → v2

Written 2026-08-07 at Phase 3 close (Task 11), HEAD `b34bc57` (675 tests
green, CI run `31187202020` green). This is the sign-off document for
**Phase 7 cutover** (`docs/superpowers/specs/2026-08-06-midicrt2-design.md`
§9) — nothing in v1 should be missing from this table by omission.

## Method

Built from three sources, cross-checked against each other rather than
trusted individually:

1. **v1's own canonical inventory** — `~/codex/midicrt/README.md`'s Pages
   table (IDs 0–17) and Plugins table, confirmed live against the actual
   loader (`midicrt.py::load_pages`/`load_plugins`, both a sorted
   `glob.glob("*.py")` over `pages/`/`plugins/` — anything with a different
   suffix, e.g. `.py.bak`/`.pybak`/no extension, never loads and is dead).
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
| 0 | Help / Keys (`help.py`) | — | **Not ported — gap.** Static keybinding reference. No v2 keymap/key→action table exists yet at all (`phase3-notes.md`'s "Input layer needs a key→action TABLE" — Phase 4). A help page has nothing to display until that table exists. |
| 1 | Notes (`notes.py`) | `pages/harmony.py` (page `harmony`) | **Ported**, Task 5. v1's `notes.py` is the primary UI surface for `zharmony.py`'s chord/scale/key/tension data; v2's `harmony` page mirrors its fields (chord 1st–4th candidates, scale candidates, inside/outside, key, tension, harmonic rhythm, motif). |
| 2 | Send Notes (`sendnotes.py`) | — | **Not ported — gap.** An interactive MIDI-output tool (plays notes from the keyboard) — real I/O from a page, not a monitor/analyzer. No phase-3 task addressed it; nothing in v2's architecture (pure analyzers, dumb renderers) currently has a slot for a page that itself emits MIDI. |
| 3 | Transport (`transport.py`) | `overlay.status` (Task 3) + `overlay.timesig` secondary chrome row (Task 6) | **Folded into chrome.** v1's dedicated Transport page (BPM/bar/beat, time signature) is not a page in v2 — its content is chrome shown on *every* page instead of one page you switch to. Deliberate synthesis, disclosed in Task 6's report ("v1 only shows this on its Transport page; v2 shows it on every page"). |
| 4 | CC Monitor (`ccmonitor.py`) | — | **Not ported — gap.** Recent-CC-per-channel table. No v2 analyzer/page exists for raw CC monitoring; `engine/midi_in.py` does pass `control_change` events through, so the wiring is possible, just unbuilt. |
| 5 | CC Dashboard (`ccgraph.py`) | — | **Not ported — gap.** A second, "perfectly aligned" CC display; also depends on `pages/legacy_contract_bridge.py`, a v1 UI-framework shim with no v2 analogue. |
| 6 | Event Log (`eventlog.py`) | `pages/eventlog.py` (page `eventlog`) | **Ported**, Task 1/3. v2's default page; clock ticks explicitly suppressed (no spam) per Task 3. |
| 7 | Program Changes (`proglog.py`) | — | **Not ported — gap.** Rolling program-change event log. `engine/midi_in.py::translate()` gained `data1` population for `program_change` in Task 10 (needed for img2txtviz's charset-offset feature) but no page consumes it as its own log. |
| 8 | Piano Roll (`pianoroll.py`) | `pages/pianoroll.py` (page `pianoroll`) | **Ported**, Task 7. Both v1 projection concepts (`pianoroll.py`'s "beat" mode and `pianoroll_exp.py`'s tempo-relative math — see ID 16) merged into one page with a `pianoroll.projection {mode}` action toggling `"tempo-relative"`/`"wallclock"`. |
| 9 | Audio Spectrum (`audiospectrum.py`) | `pages/spectrum.py` (page `spectrum`) | **Ported**, Task 8, against **real hardware** (USB C-Media audio interface, live PipeWire capture) — not a placeholder. v1's interactive retuning keys (bins/gain/smoothing/floor/ceil/freq-scale/agg-mode/lowcut/HPF-toggle/device-cycle) not ported — no v2 keybinding-table infra yet (same Phase-4 gap as ID 0); only `audio_device`/`spectrum_bins` are config-adjustable. Peak-hold is a disclosed v2 *addition* (v1 has none). |
| 10 | Tuner (`tuner.py`) | `pages/tuner.py` (page `tuner`) | **Ported (math only), inert pending audio wiring**, Task 6, assessed viable Task 8. Pure post-detection math (`freq_to_note`, `tuning_meter`, smoothing/gating) ported; the audio→pitch pipeline is NOT wired (`on_pitch_sample()` has no caller yet). Task 8 confirmed `aubio` installs cleanly on this exact Pi and a working `AudioCapture` now exists, but activating it needs a second consumer tap on that capture — small, scoped, explicitly NOT done. Registered but deliberately excluded from the default page roster (would only ever show "Listening..." until wired). |
| 11 | Chord+Key (`chordkey.py`) | `pages/harmony.py` (page `harmony`) — partial | **Not separately ported — consolidated.** `chordkey.py` is a second, more compact display of the same `zharmony.py`/`harmony.py` chord+key data that `notes.py` (ID 1) already shows in full; v2 ports the primary (`notes.py` → `harmony` page). `chordkey.py`'s own distinct compact layout has no v2 equivalent. |
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
| `loopprogress.py` | `analyzers/loopprogress.py`, `overlay.loopprogress` | **Ported (bar/beat half only)**, Task 9. The 8-cell progress bar ported (`TOTAL_BEATS=32` reproducing v1's `TOTAL_TICKS=768` at v2's beat-only clock granularity). v1's *other* half — scheduler-health + recent-SysEx diagnostic text drawn to the left of the bar — **not ported — disclosed**: no v2 scheduler-health metric and no SysEx event type surfaced by `engine/midi_in.py` (see `sysex.py` below). |
| `meters.py.bak` | — | **Intentionally dropped: dead code.** Never loaded (`.bak` suffix); per-channel velocity+note inline display, superseded/disabled in v1 itself. Found during this task's inventory sweep, not mentioned in any prior task report. |
| `pagecycle.py` | `behaviors/pagecycle.py` | **Ported, re-interpreted**, Task 9 (disclosed, not a literal port — the task brief's own explicit contract). v1: unconditional interval-based rotation through a *curated subset* (`cycle_pages=[1,6,8,9]`), suppressed only by a recent **keypress**. v2: idle-*triggered* (no MIDI activity for `pagecycle_idle_s`) cycling through the **whole roster**, since MIDI activity is the only engine-observable "someone's using this" signal and v2's roster has no page-ID subset to hardcode against. `cycle_pages`/`user_pause` have no v2 analog. |
| `polydisplay.py` | `analyzers/voices.py` | **Merged**, Task 4 (see `zvoicemonitor.py` row — both merge into one analyzer). |
| `polydisplay.py.bak` | — | **Dead code**, never loaded (co-exists with the live `polydisplay.py`; found during this sweep, harmless). |
| `sysex.py` | — | **Not ported — gap.** A real, actively-used production feature: a SysEx command receiver enabling remote control from the Cirklon sequencer (page switch, screensaver on/off, pagecycle enable/disable, capture-dump, capability query over a versioned SysEx frame format) — confirmed live via `sysex.d/` containing real captured message files, not vestigial. No phase-3 task addressed it; v2's action-API architecture is a natural transport target for this (every capability is already a named action) but **no SysEx binding layer is planned in the Phase 4 design** (`bindings.toml` covers note/CC match specs only) — flagged here explicitly as something the user should decide on, not silently assumed to be covered by Phase 4's MIDI-learn work. |
| `timeclock.py` | `analyzers/transport.py`, `overlay.status` | **Ported**, Task 3. BPM computed exactly (`60/(ts - clock_batch_start)`, no smoothing) rather than v1's smoothed estimate — a disclosed improvement, not a regression. |
| `zharmony.py` (+ top-level `harmony.py` matching engine) | `analyzers/harmony.py`, `analyzers/theory.py` | **Ported**, Task 5. `config/chords.csv`/`config/scales.csv` copied verbatim to `assets/chords.csv`/`assets/scales.csv`, byte-diffed to confirm. Several deliberate simplifications disclosed inline in `analyzers/harmony.py`'s docstring (anti-flicker tension-hold timing, dead legacy-entry filtering). |
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
- `pages/sendnotes.py` (ID 2) — interactive MIDI-send tool, not a monitor.
- Page 12 Stuck Heatmap, Page 15 TimeSig Exp (`ztimesig_exp.py`) — historical/experimental features, disclosed as future opportunities not lost logic.
- v1's interactive per-page retuning keys (spectrum's bins/gain/etc., pianoroll's channel-visibility editor, img2txtviz's gamma/ramp keys) — blocked on the Phase 4 keymap table, not silently lost; will return once that infrastructure lands, if desired.
- `zstucknotes.py`'s `PANIC_ON_CRIT` auto-MIDI-panic-send, `zvoicemonitor.py`'s poly-limit-exceeded warning log, `loopprogress.py`'s scheduler-health/SysEx diagnostic text — analyzer purity ("no I/O") and missing VM contract fields, all disclosed at the task that made the call.
- CC64 sustain-pedal handling — v1 never had it either; not a regression.
- v1's config-editing UI (`configui.py`, page 14) — v2's config page is deliberately **read-only**, a permanent design decision (never repeat the v1 settings-clobber incident), not a temporary gap.

**Accept as DEFERRED** (real gaps or later-phase work, on an explicit future plan):

- Pages 0, 4, 5, 7, 11 (Help, CC Monitor, CC Dashboard, Program Changes, Chord+Key) — found by this task's inventory sweep, addressed by **no** phase-3 task. Real gaps, not yet scheduled.
- `plugins/sysex.py` — the Cirklon SysEx remote-control receiver. Real, actively-used v1 feature with **no v2 equivalent and no plan to build one** in the current phase roadmap. Needs an explicit decision (build a SysEx binding layer alongside Phase 4's MIDI-learn work, or accept the loss of Cirklon remote control at cutover).
- Tuner audio wiring (page 10) — math ported, capture path proven working (Task 8), just not connected; small scoped follow-up.
- Web client (`midicrt-web`) — fully built (221 tests) on an isolated branch, review/merge deferred to Phase 6.
- Capture/replay, MIDI-learn/keymap — Phase 5 and Phase 4 respectively, per the original design spec's phasing; untouched by design, not oversight.
- **REAL-REBOOT audio verification** — must happen at the Phase 7 cutover window (§7 above), not before.
