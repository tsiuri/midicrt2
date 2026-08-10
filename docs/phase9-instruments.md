# Phase 9 (Instruments & Tools) — feature reference

Phase 9 plan: `~/projects/pivisualizer/docs/superpowers/plans/2026-08-09-midicrt2-phase9-instruments.md`
(ops repo, motherbase). Ledger of every task's commits/review outcomes:
`~/projects/pivisualizer/.superpowers/sdd/2026-08-09-midicrt2-phase9-instruments/progress.md`
(ops repo). Per-task reports with full design rationale and live-verification
transcripts: `task-0-report.md` .. `task-6-report.md` in that same directory —
this doc is a *summary/reference*, not a replacement; read the task report for
the "why", this doc for "what shipped and how to configure it."

Base at phase close: commit `2117288`, 1992 tests (1991 + one pre-existing
timing flake that passes in isolation, see "Known limitations" below).

## Feature summary

### Task 0 — Digit navigation → v1 page IDs
`DEFAULT_KEYMAP`'s digit/shifted-digit keys (`engine/keymap.py`) now
regenerate from `analyzers/marquee.py::PAGE_IDS` (the marquee's own single
source of truth for "v1 page ID ↔ v2 page name") as `page.goto {name}`
entries, so pressing "8" jumps to whatever page the marquee's own
`[8:PIANOROLL]` label names — matching v1's real keymap instead of the
prior roster-positional scheme. Formula: unshifted digit key = v1 ID's ones
digit for IDs 0–9; IDs 10–19 use the SAME digit character, SHIFTED (e.g. ID
11/"chordkey" → `!`, ID 10/"tuner" → `)`). One disclosed deviation: v1
reaches "tuner" via a dedicated `t`/`T` key; this scheme reaches it via
shift+0 instead, following the same formula every other ID uses (no global
letter-keyed page jump was reintroduced). `page.jump {position: int}`
(roster-positional) is untouched and still fully dispatchable — a
hand-written `keymap.toml` can restore roster-positional digits by
overriding individual keys back to `page.jump` entries. Full table: this
phase's `task-0-report.md`; schema reference: `docs/phase4-bindings.md`.

### Task 1 — Device-identity bindings
`engine/midi_identity.py::IdentityResolver` resolves each ALSA source to a
stable identity string via a ladder: USB `vendor:product[:serial]` (walked
exactly one sysfs level, interface → device, sanity-guarded by `idVendor`
presence — an unbounded walk would misattribute the *host's* USB serial to
every device) → `virt:<client>:<port>` fallback for anything without a
`,card=N` (kernel virtuals, the daemon's own ports, every `RtMidi{In,Out}
Client`). `BindingMatch` gained an optional `device` field; when present it
is the SOLE port-identity check (not ANDed/ORed with `port_pattern`) —
`bindings.toml` entries written before this task keep matching on
`port_pattern` exactly as before (`device` absent → `None`). `bind.learn`
now captures both fields on every new binding; `bind.list` shows
`device`/`device_present` alongside the pre-existing `port_pattern`/
`port_present`. Relearning the *same* control replaces the binding in
place (`should_replace_on_relearn`) rather than duplicating it — the
task's own acceptance criterion, live-verified again in this phase's T7
smoke (Smoke 2). No-serial USB devices still collide (disclosed, not
fixable without hardware serials). The `usb:` rung has never been
live-verified against real USB MIDI hardware on this Pi (none exists) —
covered by fakes only; flagged as the one open item if that hardware ever
arrives.

### Task 2 / 2b — Panic-send, stuck-linger, poly-limit log
Three v1-parity features, all config-gated:
- **Panic-send** (`config.panic_on_crit`, default `False`): on a stuck
  note's escalation to `"crit"`, the engine sends a channel-scope MIDI CC
  123 (All Notes Off, v1's own default `PANIC_SCOPE="channel"` — the
  `"all"` alternative isn't ported) via the real `MidiOutput`, AND
  synthesizes an internal release (`origin="alert"`, new provenance value
  this phase — see below) so the stuck-alert clears in the SAME tick,
  matching v1's real `eng.request_release()` pairing exactly (v1 does
  both; the original v2 draft only sent CC123 externally, which left the
  CRIT alert pinned forever since the engine itself ignores its own
  CC123 — fixed in the T2 review round). Per-channel cooldown
  (`_PANIC_COOLDOWN_S = 3.0`, hardcoded, matches v1) prevents repeat
  sends.
- **Stuck-linger** (`config.stuck_hold_after`, default `15.0`, v1's own
  `HOLD_AFTER`): a cleared stuck alert lingers, dimmed, in the chrome row
  for this many seconds. Single-slot semantics (`_last_cleared`/
  `_cleared_at` in `analyzers/stucknotes.py`) — a NEW clear batch
  overwrites whatever was lingering, exactly matching v1's own
  `_last_message_time` single-value model (not a bug; live-verified in
  T7 Smoke 3, where a second channel's clear batch visibly evicted the
  first channel's still-lingering entry).
- **Poly-limit log** (`config.poly_limit_global` default `16`,
  `config.poly_limit_ch` default `8`, v1's own values): exceeding either
  limit appends an `"instant"`-tagged event to `analyzers/voices.py`'s
  rolling log (v1's `zvoicemonitor._events` semantics, `EVENT_LOG_LEN=8`
  hardcoded) and drives a chrome-row flash (`FLASH_DURATION_S=0.5`) — v1
  itself has **no chrome rendering at all** for this data (only the
  separate voicemon PAGE shows it), so the flash is a disclosed, v2-native
  addition. The "sustain" tag (a second notification for staying over
  limit N beats) is NOT ported — out of this task's named scope.
- **Task 2b** (shutdown-hang fix): under an ALSA MIDI-input error storm,
  the asyncio signal wakeup self-pipe could saturate and drop the SIGTERM
  wakeup byte, causing `midicrtd` to require a SIGKILL from systemd.
  Fixed via a private-pipe `ShutdownWatchdog` signal path +
  `_MAX_BURST_PER_TICK=500` + `TimeoutStopSec=20` belt-and-suspenders.
  Unrelated to the config knobs above but discovered during T2's live
  verification; folded in as its own task.

### Task 3 — Live tuner
`analyzers/tuner.py`: numpy YIN pitch detection (aubio proven non-viable on
this aarch64 venv — investigated first, per the task's own brief) over the
existing 44.1kHz/1024-sample audio stream (43Hz cadence). Detection
constants (hardcoded, not config — matches this codebase's "only the
brief's named knob is configurable" precedent):

| Constant | Value | Why |
|---|---|---|
| `TUNER_FMIN_HZ` | 65.0 | Caps `tau_max` at ~66% of the block to avoid edge-instability false-positives near Nyquist; a bass low E1/B0 falls below this floor (disclosed range limit). |
| `TUNER_FMAX_HZ` | 1500.0 | Comfortably above typical fundamentals; harmonics filtered by the difference-function shape itself. |
| `YIN_THRESHOLD` | 0.15 | The original YIN paper's own default. |
| `MIN_CONF` | 0.65 | A 500-seed white-noise sweep at this block size never exceeded confidence 0.61 (p99 0.588) — 0.65 leaves headroom above the measured noise ceiling. |

Audio capture is **demand-gated** (current-page OR topic-subscriber
refcount, shared mechanism with `spectrum`, extended to both pages in the
T3 review round): idle floor ≈2.65–2.9% of one core (was ~39–40% with
audio always-on), spectrum-current ≈13–19%, tuner-current ≈15.8–16% (both
live-verified again in T7 Smoke 4, production: idle 3%, tuner-current
16%). Debounced stop (`_AUDIO_CAPTURE_STOP_DEBOUNCE_S`) tolerates a brief
rotate-through without re-opening the audio device. v1's header status
line (`Dev:<name>`) is ported into the page header, matching `spectrum`'s
own established convention.

### Task 4 — Web reconnect + control posture + tab nav
`clients/web/bridge.py`'s `Bridge`: an engine-socket EOF (e.g. `midicrtd`
restart) now triggers a bounded/jittered backoff reconnect loop instead of
permanently freezing every browser tab — sinks (websockets) are kept OPEN
throughout (Option B, user ruling), a `bridge_status` event
(`reconnecting`/`connected`) is pushed to every open tab, and on
reconnect every topic is freshly re-subscribed (stale pre-outage snapshots
are never replayed). Live-verified again in T7 Smoke 5 against an isolated
scratch pair: `bridge_status: reconnecting` immediately on daemon kill,
`bridge_status: connected` ~22s later (default production backoff
constants) after the daemon restarted, fresh `seq: 1` snapshots delivered
on the SAME websocket connection. `midicrt-web`'s control posture inverted:
`--allow-control` (opt-in) → `--read-only` (opt-out) — control is ON by
default (user ruling: "web control ON, no auth"); `packaging/
midicrt-web.service` needed no flag change since it never named
`--allow-control`. Tab-click navigation dispatches `page.goto` as an
`origin="client"` action (same as a real keypress), which correctly arms
`pagecycle_user_pause` — live-verified in T7 Smoke 5 (a web-dispatched
`page.goto` held the page steady across 4+ scratch `pagecycle_interval`
windows with zero auto-rotation).

### Task 5 — SysEx manager
`engine/sysex_store.py`: every incoming sysex frame (midicrt-addressed or
not) is recorded into a ring of recents; `sysex.save {name, index}` copies
one into a named library entry under `sysex_dir` (config key, default
`/var/lib/midicrt/sysex`, a `StateDirectory` sibling of `capture_dir`).
Actions `sysex.list/save/play/delete` are ordinary registered actions,
reached through the SAME generic `/api/action` HTTP endpoint every other
web action uses — no separate route. `sysex.play` sends the saved frame
out the real `MidiOutput` (provenance-marked); `sysex.delete` stages to a
`trash/` subdir (stage-don't-delete, refuses on a name that doesn't
exist). Full round-trip (inject → recent ring → save → play → independent
second-port wire observation → delete-to-trash) live-verified again in T7
Smoke 5, this time through the actual web `/api/action` HTTP layer rather
than the CLI. Name sanitization rejects path traversal
(`sanitize_name`, regex-bounded at 63 chars, red-teamed including a
symlink attack during the T5 review). Web panel: a `#sysex-panel` behind
`allow_control`, polling `sysex.list` plus reacting to `sysex_received`
protocol events (deliberately unrate-limited — disclosed, see
`engine/sysex_store.py`'s module docstring). Chrome secondary-row
sysex-status text is fed EXCLUSIVELY by `engine/sysex.py`'s pre-existing
CMD-dispatch outcome text (v1's real `plugins/sysex.py:55-57` semantics —
only an actual midicrt remote-control command outcome lights this row,
never generic foreign sysex traffic or the sysex MANAGER's own save/
play/delete actions) — see "Chrome priority chain" below for where it
sits.

### Task 6 — Capture editor
`midicrt sessions {list,show,trim,repair-index,delete}` — a pure CLI
subcommand needing **no running daemon** for its actual work (mirrors
`replay`'s "no socket needed" shape), except a best-effort liveness probe
over `capture.status` so `trim`/`delete` never touch the session the
daemon is CURRENTLY appending to (`skipped_live` in `repair-index`'s
output, `session live, refusing` for `trim`/`delete`). `trim <id> --from
--to` (session-relative seconds) extracts a time range to a NEW session
file — the original is never mutated. Notes that were already active
BEFORE `--from` get a synthesized boundary `note_on` (marked
`"synthetic": true`, honest provenance) so the trimmed replay's totals are
correct instead of silently missing a note that was mid-sustain when the
window opened — live-verified again in T7 Smoke 6 with an exact
session-relative-timestamp-computed boundary case. `repair-index` adopts
orphan sessions (files on disk with no `index.json` row — the T2b-era
shutdown-hang's own failure signature) with metadata derived from the
file's own last real event, never fabricated. `delete` stages to
`trash/`, refuses on a pinned session. Web: a read-only `#sessions-panel`
(list + `capture.sessions_show` summaries) behind `allow_control`.
**Production write**: T7 ran `repair-index` against the real
`/var/lib/midicrt/sessions` store — see "Production repair-index" below.

The index-write lock (`index.json`'s cross-process flock) is acquired
OFF the asyncio event loop (`asyncio.to_thread` + `LOCK_NB` + retry) so a
long-running `repair-index` scan (multi-MB session files) never freezes
the daemon for unrelated requests — this was a Critical finding across two
review rounds (a blocking-lock version froze the whole daemon; the first
non-blocking fix left a `stop()`/lock-wait race that could kill the
engine's tick loop silently) before landing at the final, adversarially
stress-tested (300+ stop-race, ~11,700 start-race iterations, 0 crashes)
implementation. T7's own production `repair-index` run (12 real orphans,
one 15MB+) re-confirms this live: 26 parallel `midicrt status` probes
during the ~14s scan, 0 failures, 0.69–0.91s latency throughout (normal
CLI-startup overhead, no spike correlated with the scan).

## Config keys added this phase (all in `config.toml`, all optional)

| Key | Default | v1 source |
|---|---|---|
| `panic_on_crit` | `false` | v1 ships `true`; v2 defaults OFF — deliberate posture change, disclosed, not a restoration |
| `stuck_hold_after` | `15.0` | v1's `HOLD_AFTER` — 1:1 port |
| `poly_limit_global` | `16` | v1's `POLY_LIMIT_GLOBAL` |
| `poly_limit_ch` | `8` | v1's `POLY_LIMIT_CH` |
| `sysex_dir` | `None` (resolves to `/var/lib/midicrt/sysex`, a `StateDirectory` sibling of `capture_dir`) | new in v2 (v1 used `sysex.d/` relative to its own CWD) |

`audio_device` (pre-existing since Phase 3) is now ALSO read by
`TunerPage` (same substring-match knob `spectrum` already used) — not a
new key, but a new consumer.

No new digit-nav/device-identity config keys — both are pure keymap-table
regeneration (Task 0) and automatic identity resolution (Task 1) with no
user-facing toggle.

## Provenance origins added this phase

The dispatch-origin vocabulary (`engine/actions.py`'s "four dispatch
origins" plus the pre-existing `auto`/`shutdown`/`sysex`/`binding:<id>`
extensions) gained one new value:

- **`"alert"`** (Task 2) — the panic-send's synthesized internal note
  release (`panic.release` action mark), distinguishing an
  engine-synthesized "this note was force-released by panic" event from a
  real MIDI note-off (`origin="client"` or unmarked real input) in
  captured session files. Grep `origin.*alert` in a session's `.jsonl` to
  find every panic-forced release.

Task 6's trim boundary synthesis uses a DIFFERENT marker
(`"synthetic": true` on the event record itself, not a provenance
`origin` value) — both mechanisms exist for the same underlying reason
(mark engine-fabricated data as honestly fabricated, never silently
indistinguishable from something that was actually received), but are
distinct fields in distinct contexts (live dispatch vs. post-hoc trim
extraction) — don't conflate them when grepping capture files.

## Chrome priority chain (final state, `clients/chrome.py::secondary_status_text`)

The shared second chrome row picks the MOST urgent of, in order — **as
re-ranked in the Phase 9 close-out fix wave, controller ruling** (a
DIMMED, already-resolved historical relic must never mask live, urgent
info; this superseded an earlier draft where "stuck alerts" was one
combined rung covering BOTH the live and lingering cases, letting a
resolved-and-dimmed relic outrank a live poly-limit flash or an active
sysex confirmation):

1. **Live stuck alerts** (`STUCK WARN`/`STUCK CRIT`, currently active) —
   v1's original tenant of this row. Unconditional rung 1, no exceptions.
2. **Poly-limit flash** (Task 2, v2-native — v1 has no chrome home for
   this data at all) — same "something is wrong right now" tier as a
   live alert, but a live alert wins when both are simultaneously true.
3. **Sysex-status text** (Task 5, v1 parity item — v1 showed this on a
   DIFFERENT row entirely, the loopprogress bar's row; v2 deliberately
   consolidates it onto this shared row instead of reserving a new one,
   following the same precedent `timesig_text` already established).
   Informational/confirmatory, never as urgent as a live alert/poly-limit,
   but more timely than either the lingering-cleared relic below it or
   the routine fallback.
4. **Lingering-cleared stuck-note message** (`STUCK CLEARED: ...`, DIMMED
   — `config.stuck_hold_after`) — a resolved, historical fact, not a live
   one. Ranked BELOW poly-limit/sysex (both real, live, actionable RIGHT
   NOW) but still ABOVE the routine timesig fallback (a recently-cleared
   stuck note is more noteworthy than the always-present baseline).
5. **Timesig text** — routine fallback, wins only when nothing above is
   active.

`polylimit_vm`/`sysex_vm` are both optional args (default "not active") so
every pre-Phase-9 call site with only 2–3 args keeps working unchanged.
`secondary_status_dim()` (the renderer's "should this row be dimmed" cue)
takes the SAME two optional args now, for the identical reason: it must
agree with `secondary_status_text()` about which rung actually won, or
the row could render full-brightness poly-limit/sysex text while still
being told to paint the dimmed background underneath it (a real bug this
same fix closed, `clients/chrome.py::secondary_status_dim`'s own
docstring has the full incident).

## Known limitations

- **Tuner harmonic boundary (~5:1)**: the YIN-based detector resists a
  weak-fundamental/strong-2nd-harmonic confusion robustly at a 3:1
  amplitude ratio (fundamental correctly detected across 4 test
  fundamentals, 5 random phase offsets, confidence 1.000) but breaks down
  at more extreme ratios — empirically, somewhere between 0.15 and 0.10
  relative fundamental amplitude against a fixed 0.5-amplitude harmonic
  (~5:1+), the detector starts reporting the harmonic instead of the
  fundamental. A known YIN-family limitation at extreme harmonic
  dominance, not a defect this phase introduced — disclosed, not fixed
  (out of scope; the 3:1 case is the shipped regression pin).
- **Unbounded `Engine.queue` ingestion**: pre-existing (surfaced during
  Task 2b's shutdown-hang investigation, not itself fixed this phase) —
  under a sustained MIDI-input flood, the engine's internal event queue
  has no bound or drop policy. Task 2b fixed the SHUTDOWN half
  (`_MAX_BURST_PER_TICK=500` bounds one flush cycle); the general
  ingestion-side policy (bounded queue + drop policy, or a rate warning)
  remains open — candidate for Phase 10 or a dedicated follow-up task.
- **`usb:` device-identity rung** (Task 1): verified only against fakes
  (`tests/test_midi_identity.py`) — no real USB MIDI interface exists on
  this Pi to live-verify against. The `virt:` rung IS live-verified
  (real `Midi Through` traffic, T1's own report + this phase's T7 Smoke
  2 with an isolated virtual port).
- **Known test flake**: `tests/test_web_bridge.py::
  test_bridge_follows_page_changed_and_resubscribes` — a pre-existing
  timing flake (same family as an earlier CI race fixed in `e757e63`),
  passes reliably standalone/on rerun, occasionally flags red in the
  full-suite run. Re-confirmed flaky-not-broken during T7's baseline run
  (failed in the full suite, passed twice in a row standalone
  immediately after). Candidate for a deflake pass or an explicit
  known-flakes list — not fixed this phase.
- **`_tick_audio_gate` no-assert guard** (Task 3 fix round): `PageHooks`
  discovery silently skips a page that defines `start_capture` without
  `stop_capture` (or vice versa) instead of asserting. Both `spectrum`
  and `tuner` correctly define both today, so this is inert — a guard
  clause is warranted if a third capture-owning page ever lands without
  both hooks.
- **Web bridge / sysex panel** are exercised via raw HTTP+WebSocket
  clients (curl, a Python `aiohttp` client) throughout this phase's live
  verification, not a real browser — no browser/JS test harness exists in
  this project (every prior phase's own precedent; the Pi is headless).

## Production repair-index (T7, the one sanctioned production write)

`midicrt sessions repair-index` was run against the real
`/var/lib/midicrt/sessions` store on 2026-08-10, adopting 12 pre-existing
orphan sessions (Task-2b-era shutdown casualties — sessions left without
an `index.json` row by the ALSA-error-storm SIGKILL bug T2b fixed, plus
two from this task's own testing window's daemon restarts). Before: 51
sessions total (12 orphan, 38 finished, 1 recording). After: 51 total (0
orphan, 50 finished, 1 recording) — 0 dropped, the live-recording session
correctly skipped/untouched throughout. One spot-checked orphan (15MB,
72,070 real `note_on` events) shows its adopted `ended_ts` matches its own
last real event's timestamp exactly — honest, derived metadata, nothing
fabricated. Daemon responsiveness during the ~14s scan verified via a
parallel `midicrt status` probe (26 samples, 0 failures, 0.69–0.91s
latency, no spike). Full before/after JSON + probe log:
`~/projects/pivisualizer/docs/evidence-phase9-smoke/` (ops repo,
motherbase).

**Follow-up run, Phase 9 close-out fix wave** (same day, after Task 6's
own three review rounds — index.json locking, panic-release replay
parity, chrome re-rank, etc. — and one more deliberate production
restart to deploy them): `midicrt sessions repair-index` run a SECOND
time, this time genuinely idempotent-clean rather than a real adoption --
**0 adopted, 0 dropped, 51 kept** (every one of T7's own 51 finished
sessions, verbatim), and the store's THEN-current live session correctly
skipped. Store grew to 52 total by the time of this run (one more
finished session landed from the fix wave's own live verification
activity, cleanly indexed at `capture.stop` time, never orphaned) — **the
store's steady-state orphan count is 0 both before and after this
entire fix wave**, not the "a fresh orphan is expected" the coordinator's
own instructions predicted going in: every `capture.stop`/`capture.start`
cycle exercised during this round's own live checks (§10/§11 of `task-6-
report.md`, plus this wave's own final-wave-report.md) completed cleanly
(including through a real, deliberate production restart), so nothing
was left to adopt. Disclosed as the honest result, not silently
adjusted to match the prediction.
