# midicrt2 — prior-art index (archived 2026-09-20)

midicrt2 was an experimental ground-up rewrite of the midicrt CRT visualizer. **It is
discontinued** — the owner didn't like where it went, and the system is developed by patching
**midicrt v1** (https://github.com/tsiuri/midicrt) in place. This file exists so that when a
feature comes up that was *already designed, built and tested here*, it can be found and mined
as prior art instead of being reinvented. 127 commits, ~70 test files, ~2000 tests, all green
at the time it was shelved.

**Mine it, don't revive it.** v2 is an engine-daemon + protocol + thin-clients architecture;
v1 is a single process with pages and plugins. Code rarely transplants whole — take the
algorithm, the edge cases the tests pin down, and the documented gotchas.

## Where the code lives now

| Copy | Location | Notes |
|---|---|---|
| GitHub (read-only, archived) | https://github.com/tsiuri/midicrt2 | full history |
| motherbase | `~/projects/deprecated/midicrt2/` | full clone **plus** the untracked `.superpowers/sdd/` task briefs + reports (design reasoning per task); in the nightly backup |
| Pi | `~/deprecated/midicrt2-2026-09-20/` | repo, its venv, `~/.config/midicrt` (config.toml, bindings.toml), `/var/lib/midicrt` (199 MB of captured sessions), the three systemd unit files |
| Design + plans | motherbase `~/projects/pivisualizer/docs/superpowers/{specs,plans}/*midicrt2*`, `docs/phase*-notes.md`, `docs/gui-phase-decisions-2026-08-08.md` | the *why* behind each phase |

## Fishing guide — "I want X; did we already build it?"

| If you want… | Look at | Why it's worth reading |
|---|---|---|
| **MIDI events triggering actions** (notes/CCs → page change, panic…) with **MIDI-learn** | `src/midicrt/engine/bindings.py`, `docs/phase4-bindings.md` | DAW-style learn, replace-on-relearn, CC-edge baselines, durable TOML; v1's knobctl control mode is the hand-rolled cousin |
| **Remappable keyboard shortcuts**, per-page key sections, a help overlay generated from the map | `src/midicrt/engine/keymap.py`, `docs/phase4-bindings.md` §1–1c | schema v2 with `[keys.<page>]`, args-table entries, digit nav tied to v1 page IDs |
| **Telling two identical MIDI devices apart / surviving port renumbering** | `src/midicrt/engine/midi_identity.py` | `usb:vendor:product:serial` ladder, relearn dedup, pattern rescue |
| **MIDI input that recovers from ALSA overflow** instead of wedging | `src/midicrt/engine/midi_in.py` | rtmidi error callback → close+reopen at poll cadence; fixed a real permanent-wedge bug |
| **Recording a session and replaying it** (event-sourced) | `src/midicrt/engine/capture.py`, `engine/replay.py`, `docs/phase5-capture.md` | JSONL of raw MIDI + provenance-tagged action marks; offline replay engine |
| **Trimming / repairing / listing captured sessions** | `src/midicrt/engine/sessions.py` | flock'd index safe against a live daemon; trim synthesizes boundary state |
| **SysEx librarian** — record, save, replay sysex from the browser | `src/midicrt/engine/sysex_store.py`, `engine/sysex.py`, `docs/phase9-instruments.md` | bounded ring + on-disk library, path-traversal-proof |
| **Panic / stuck-note healing** | `src/midicrt/analyzers/stucknotes.py`, `engine/core.py` (release handling), `docs/phase9-instruments.md` | CC123 + synthetic release, stuck-linger, poly-limit log |
| **A tuner that doesn't cost 40% CPU** | `src/midicrt/analyzers/tuner.py`, `analyzers/spectrum.py` (`AudioCapture`) | numpy-YIN (aubio is non-viable on aarch64); **demand-gated audio capture**: ~2.6% idle vs 40% always-on |
| **A web UI that controls the visualizer**, survives daemon restarts | `src/midicrt/clients/web/bridge.py`, `clients/web/app.py`, `docs/phase6-web.md` | auto-reconnect bridge, read-only vs control posture, security rationale |
| **Clean shutdown in ~2 s** under asyncio + threads | `src/midicrt/daemon.py` (ShutdownWatchdog) | the shared-self-pipe saturation bug, proven and fixed |
| **Fast RGB565 framebuffer drawing, PSF fonts, a monochrome luminance ramp** | `src/midicrt/clients/fb/surface.py`, `fb/text.py`, `fb/lum.py` | `to_rgb565` perf work; the "monochrome mandate" palette |
| **A status bar shared by several front-ends** | `src/midicrt/clients/chrome.py` | renderer-agnostic footer/header logic |
| **Config with a schema** (and the validation gap that was found) | `src/midicrt/config.py` | read-only TOML; the missing type validation was queued as "Phase 10" debt — don't copy that hole |
| **Every v1 page re-expressed as pure analyzer + thin page** | `src/midicrt/analyzers/*`, `src/midicrt/pages/*`, `docs/phase3-parity.md`, `docs/visual-audit.md` | clean, wall-clock-injected, unit-tested versions of harmony, timesig, voices, spectrum, pianoroll, img2txtviz… good reference when fixing the v1 originals |
| **A JSON-lines control protocol over a unix socket** | `src/midicrt/proto.py`, `engine/server.py`, `clients/base.py`, `clients/cli.py` | request/response + latest-wins snapshot push |
| **Page auto-cycling / screensaver semantics** | `src/midicrt/behaviors/pagecycle.py`, `behaviors/screensaver.py` | v1 semantics restored verbatim, with tests |

## Every module, one line each

| File | Lines | What it is |
|---|---:|---|
| `src/midicrt/analyzers/beatflash.py` | 150 | BeatFlashAnalyzer: beat-synced flash pulse, ported from v1's `~/codex/midicrt/plugins/beatflash.py` (READ-ONLY reference on the Pi). |
| `src/midicrt/analyzers/ccmonitor.py` | 181 | CCMonitorAnalyzer: raw MIDI CC (control-change) tracking, ported from v1's `~/codex/midicrt/pages/ccmonitor.py` (PAGE_ID 4, "CC Monitor", 36 lines) and `~/codex/midicrt/p |
| `src/midicrt/analyzers/harmony.py` | 651 | HarmonyAnalyzer: chord/scale/key/tension/harmonic-rhythm/motif detection, ported from v1's `~/codex/midicrt/plugins/zharmony.py` (563 lines — the chord/scale history, key |
| `src/midicrt/analyzers/img2txtviz.py` | 308 | Img2TxtVizAnalyzer: v1's `pages/img2txtviz.py` (552 lines, READ-ONLY reference on the Pi) ported as a pure, wall-clock-injected state machine -- see docs/phase3-notes.md' |
| `src/midicrt/analyzers/loopprogress.py` | 119 | LoopProgressAnalyzer: an 8-bar cyclic position marker, ported from v1's `~/codex/midicrt/plugins/loopprogress.py` (READ-ONLY reference on the Pi). |
| `src/midicrt/analyzers/marquee.py` | 314 | MarqueeAnalyzer: the header page-title scrolling marquee, ported from v1's `~/codex/midicrt/midicrt.py` (READ-ONLY reference on the Pi), row 0 of its `_ui_loop_body()` -- |
| `src/midicrt/analyzers/spectrum.py` | 654 | SpectrumAnalyzer + AudioCapture: v1's audio spectrum analyzer, ported from `~/codex/midicrt/pages/audiospectrum.py` (733 lines, READ-ONLY reference on the Pi) -- see docs |
| `src/midicrt/analyzers/stucknotes.py` | 388 | StuckNotesAnalyzer: long-held/orphaned-note detection, ported from v1's `~/codex/midicrt/plugins/zstucknotes.py` (READ-ONLY reference on the Pi). |
| `src/midicrt/analyzers/theory.py` | 359 | theory.py — chord/scale pitch-class matching engine. |
| `src/midicrt/analyzers/timesig.py` | 395 | TimesigAnalyzer: heuristic time-signature estimation, ported from v1's `~/codex/midicrt/plugins/ztimesig.py` (READ-ONLY reference on the Pi). |
| `src/midicrt/analyzers/transport.py` | 183 | TransportAnalyzer: BAR/BEAT/BPM/running/clock-source, ported from v1's `~/codex/midicrt/plugins/timeclock.py` (display) and the transport engine it reads from, `~/codex/m |
| `src/midicrt/analyzers/tuner.py` | 384 | TunerAnalyzer: pitch-reference display, ported from v1's `~/codex/midicrt/pages/tuner.py` (READ-ONLY reference on the Pi). |
| `src/midicrt/analyzers/voices.py` | 311 | VoiceMonitorAnalyzer: per-channel polyphony + active-note tracking, ported from v1's `~/codex/midicrt/plugins/zvoicemonitor.py` (poly counts + peak hold) and `~/codex/mid |
| `src/midicrt/behaviors/pagecycle.py` | 368 | PageCycleBehavior: v1's `~/codex/midicrt/plugins/pagecycle.py` semantics, RESTORED VERBATIM. Phase 8 Task 5 (docs/superpowers/sdd/ 2026-08-08-midicrt2-phase8-gui/task-5-b |
| `src/midicrt/behaviors/screensaver.py` | 189 | ScreensaverBehavior: idle-triggered burn-in guard, ported (with a disclosed re-interpretation of the ACTUATION mechanism) from v1's `~/codex/midicrt/plugins/zscreensaver. |
| `src/midicrt/clients/base.py` | 566 | Shared client library: connect/hello/subscribe handshake + id-correlated request/response over the midicrt engine protocol. |
| `src/midicrt/clients/chrome.py` | 854 | Chrome: the shared, renderer-agnostic status-bar logic both clients wrap. |
| `src/midicrt/clients/cli.py` | 347 | midicrt — protocol client CLI (also the debugging tool). |
| `src/midicrt/clients/fb/app.py` | 2448 | fb/app.py -- midicrt-fb: the framebuffer CRT client entrypoint. |
| `src/midicrt/clients/fb/lum.py` | 149 | fb/lum.py — the monochrome green-luminance framework (Phase 8 Task 2). |
| `src/midicrt/clients/fb/surface.py` | 261 | fb/surface.py — RGB pixel surface + RGB565 framebuffer packing. |
| `src/midicrt/clients/fb/text.py` | 232 | fb/text.py — PSF bitmap font loading and text rendering onto a Surface. |
| `src/midicrt/clients/tui.py` | 1469 | Minimal TUI client: renders the current page, sends actions. |
| `src/midicrt/clients/web/app.py` | 191 | midicrt-web -- aiohttp HTTP/WS wiring around Bridge (see bridge.py for the EngineClient<->asyncio fan-out design this module is just plumbing for). |
| `src/midicrt/clients/web/bridge.py` | 748 | EngineClient <-> asyncio fan-out bridge for midicrt-web. |
| `src/midicrt/config.py` | 269 | User config: read-only TOML (spec §7 — the engine never writes this file). |
| `src/midicrt/daemon.py` | 202 | midicrtd — engine daemon entrypoint. |
| `src/midicrt/engine/actions.py` | 195 | Central action registry: every engine capability is a named action (spec §4). |
| `src/midicrt/engine/bindings.py` | 966 | MIDI binding runtime (Phase 4 Task 2, docs/phase4-notes.md): lets a raw MIDI event (not a client keypress) fire an engine action, with zero clients attached -- the "seque |
| `src/midicrt/engine/capture.py` | 935 | Event-sourced session capture (Phase 5 Task 1, docs/phase5-notes.md): `CaptureSink` records raw MIDI + provenance-tagged action marks to a per-session JSONL file, for a f |
| `src/midicrt/engine/core.py` | 3478 | Engine: owns MIDI event flow, pages, actions; publishes snapshots (spec §2). |
| `src/midicrt/engine/keymap.py` | 662 | Config-served keymap -- Phase 4 Task 1 (docs/phase4-notes.md) built the original single `[keys]` TOML table mapping one-character keys to action names; Phase 8 Task 6 (do |
| `src/midicrt/engine/midi_identity.py` | 274 | Device-identity resolution for MIDI sources (Phase 9 Task 1, .superpowers/sdd/2026-08-09-midicrt2-phase9-instruments/task-1-brief.md). |
| `src/midicrt/engine/midi_in.py` | 439 | MIDI input: watch ALSA seq ports matching config patterns, feed the engine queue. |
| `src/midicrt/engine/midi_out.py` | 187 | MIDI output: a single, lazily-opened engine-owned virtual port used by TWO phase-3 task 12 gap ports -- `pages/sendnotes.py` (v1's interactive note-sender, PAGE_ID 2, the |
| `src/midicrt/engine/replay.py` | 589 | Session replay through an OFFLINE `Engine` (Phase 5 Task 2, docs/ phase5-notes.md): streams a captured session's `.jsonl` (see engine/ capture.py's own module docstring f |
| `src/midicrt/engine/server.py` | 259 | Unix-socket protocol server: request/response + latest-wins snapshot push. |
| `src/midicrt/engine/sessions.py` | 851 | Capture-store maintenance (Phase 9 Task 6, "capture editor"): the `midicrt sessions` CLI subcommands (`list`/`show`/`trim`/`repair-index`/ `delete`) AND the engine's own  |
| `src/midicrt/engine/sysex.py` | 122 | SysEx command PARSING (pure) — the frame-format half of v1's `~/codex/midicrt/plugins/sysex.py` (275 lines, READ-ONLY reference): the Cirklon remote-control receiver, a r |
| `src/midicrt/engine/sysex_store.py` | 491 | SysEx MANAGER (Phase 9 Task 5, user-requested NEW feature: "record, save, and play sysex at will from the browser"): a bounded RING of recently-received raw sysex frames, |
| `src/midicrt/pages/ccdashboard.py` | 38 | CC Dashboard page (page name "ccdashboard"): v1's `~/codex/midicrt/pages/ ccgraph.py` (PAGE_ID 5, "CC Dashboard" -- v1's own comment calls it "perfectly aligned") -- a si |
| `src/midicrt/pages/ccmonitor.py` | 40 | CC Monitor page (page name "ccmonitor"): v1's `~/codex/midicrt/pages/ ccmonitor.py` (PAGE_ID 4) -- a per-channel table of the last few raw CC messages. Wraps `analyzers.c |
| `src/midicrt/pages/chordkey.py` | 148 | Chord+Key page (page name "chordkey"): v1's `~/codex/midicrt/pages/ chordkey.py` (PAGE_ID 11, "Chord+Key", 116 lines, READ-ONLY reference) -- a second, more compact chord |
| `src/midicrt/pages/configview.py` | 126 | Config page (page name "config"): a READ-ONLY viewer of the engine's effective configuration plus live engine facts -- spec §5's config-page clarification: "The config pa |
| `src/midicrt/pages/eventlog.py` | 51 | Event-log page: ring buffer of recent MIDI events -> text-run view-model. |
| `src/midicrt/pages/harmony.py` | 88 | Harmony page: v1's Notes-page harmony fields (chord/scale/key/tension/ harmonic-rhythm/motif -- see docs/evidence-phase2-smoke/after.png for v1's layout: "Chord: Last/2nd |
| `src/midicrt/pages/help.py` | 124 | Help page (page name "help"): a read-only reference of what THIS build can actually do, per the task-12 brief's "parity port" guidance for v1's Help page. |
| `src/midicrt/pages/img2txtviz.py` | 80 | Img2txtviz page: wraps `analyzers.img2txtviz.Img2TxtVizAnalyzer` -- v1's `pages/img2txtviz.py` (PAGE_ID 17, "MIDI IMG2TXT"). See that analyzer module's docstring for the  |
| `src/midicrt/pages/pianoroll.py` | 1309 | Pianoroll page: v1's flagship two-projection-mode scrolling note display. |
| `src/midicrt/pages/progchanges.py` | 90 | Program Changes page (page name "progchanges"): a rolling log of program-change events, ported from v1's `~/codex/midicrt/pages/proglog.py` (PAGE_ID 7, "Program Changes", |
| `src/midicrt/pages/screensaver.py` | 47 | Screensaver page: the visual `behaviors/screensaver.py` switches to via `page.goto screensaver`, ported from v1's `~/codex/midicrt/plugins/ zscreensaver.py` (READ-ONLY re |
| `src/midicrt/pages/sendnotes.py` | 236 | Send Notes page (page name "sendnotes"): v1's `~/codex/midicrt/pages/ sendnotes.py` (PAGE_ID 2, "Send Notes", 148 lines, READ-ONLY reference) -- an interactive MIDI-outpu |
| `src/midicrt/pages/spectrum.py` | 68 | Spectrum page: v1's `pages/audiospectrum.py` (PAGE_ID 9, "Audio Spectrum"), wrapping `analyzers.spectrum.SpectrumAnalyzer` -- see that module's docstring for the full inv |
| `src/midicrt/pages/tuner.py` | 101 | Tuner page: v1's `pages/tuner.py` (PAGE_ID 10, "Tuner"), wrapping `analyzers.tuner.TunerAnalyzer` -- see that module's docstring for the full Phase 9 Task 3 investigation |
| `src/midicrt/pages/voices.py` | 72 | Voices page: v1's main screen (see docs/evidence-phase2-smoke/after.png) -- 16 instrument rows, one per MIDI channel, showing live + peak polyphony and the currently-held |
| `src/midicrt/proto.py` | 48 | Wire protocol: JSON-lines framing and message shapes (spec §3). |

## Docs in this repo

| File | Title |
|---|---|
| `docs/phase1-smoke.md` | Phase 1 smoke test (on the Pi) |
| `docs/phase2-smoke.md` | Phase 2 smoke test — supervised real-CRT run (on the Pi) |
| `docs/phase3-parity.md` | Phase 3 parity checklist — v1 → v2 |
| `docs/phase3-smoke.md` | Phase 3 Task 11 smoke — supervised all-pages real-CRT run |
| `docs/phase4-bindings.md` | Phase 4 bindings guide — keymap.toml + bindings.toml + MIDI learn |
| `docs/phase5-capture.md` | Phase 5 capture guide — event-sourced session recording + replay |
| `docs/phase6-web.md` | Phase 6 web guide — `midicrt-web`, deployment posture, live smoke evidence |
| `docs/phase8-smoke.md` | Phase 8 Task 7 smoke — supervised real-CRT run (RESOLVED) |
| `docs/phase9-instruments.md` | Phase 9 (Instruments & Tools) — feature reference |
| `docs/visual-audit.md` | v1 Visual-Feature Audit |
