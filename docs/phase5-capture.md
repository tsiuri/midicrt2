# Phase 5 capture guide — event-sourced session recording + replay

Written 2026-08-07 at Phase 5 close (Task 3), HEAD (this task's commits, on
top of `d977df7`, 1275 tests). Covers the three tasks built across Phase 5:
event-sourced **capture** to a per-session JSONL file (Task 1), **replay**
of a captured session through an offline engine (Task 2), and this task's
own **learned-binding port durability** + CLI/help cheap wins + the on-Pi
end-to-end smoke that proves the whole pipeline against real hardware.

Companion reading: `docs/phase4-bindings.md` (keymap/bindings/learn —
capture's action marks reference bindings by id, and this task's port
durability fix lives in that same subsystem) and `docs/phase5-notes.md`
(the phase's carry-over design notes this whole phase implements).

---

## 1. What gets captured, and why JSONL-append instead of v1's one-shot save

`engine/capture.py::CaptureSink` records **raw MIDI + provenance-tagged
action marks** to a per-session `.jsonl` file under
`/var/lib/midicrt/sessions/` (production) — `capture.start`/`.stop` are the
two action handlers that open/close a session; every other engine consumer
(`_handle`, the dispatch hook, three sysex handlers, page-change funnel)
feeds it as a side effect of its own normal work, not as a separate stage.

v1's authority (`~/codex/midicrt/engine/memory/capture.py::
MemoryCaptureManager`) finalizes a session as ONE atomic JSON document at
stop — v2 deliberately does not: a v2 capture is a standing recording that
can run for hours, so writing once-at-the-end would mean an unbounded
in-memory buffer and total data loss on a crash. Instead, `record_event`/
`record_action`/`record_page_changed` (the ONLY methods called from the
hot path) just append a plain dict to an in-memory `deque` — no I/O — and
`flush()` drains the whole buffer to disk in one batched `write()`+
`fsync()`, gated to roughly once per second (`maybe_flush`, called once per
engine tick). See §4 for what this trades away, disclosed rather than
hidden.

---

## 2. On-disk format: one JSON object per line, five `kind`s

Every line is a complete, self-contained JSON object (`json.dumps(...) +
"\n"`), `format: 1` (`FORMAT_VERSION` in `engine/capture.py`) recorded once
in the header. Read/parsed line-by-line — a truncated last line (mid-write
crash) or a malformed line only ever loses/skips THAT line, both in
`CaptureSink` itself reading back an index and in `engine/replay.py`'s own
`_iter_lines`.

### `header` (exactly one, always the first line)

```json
{"kind": "header", "format": 1, "session_id": "session-20260807-231807-e3e138aa",
 "started_ts": 1786169887.77, "engine_version": "2.0.0.dev0",
 "instruments": ["Kawai XD5", "Matrix-1k", ...]}
```

Written (and flushed) synchronously inside `start()`, before `capture.start`
even returns — so `ls` on the sessions directory shows the new file
immediately, not only after the first ~1s flush cadence tick.

### `event` (one per raw `MidiEvent`, ground truth)

```json
{"kind": "event", "ts": 1786169890.45, "source": "Midi Through:Midi Through Port-0 14:0",
 "type": "note_on", "channel": 0, "data1": 60, "data2": 100, "summary": "note_on ch1 n60 v100"}
```

Recorded for **every** `MidiEvent` `Engine._handle` ever sees (after the
self-output filter, before any dispatch branching) — this is deliberate:
raw MIDI is ground truth (docs/phase5-notes.md's decided design), and
action marks below record what FIRED as a *separate* fact, not a
replacement for the raw trace. `clock_tick` lines additionally carry
`clock_batch_start`; `sysex` lines carry `sysex_data` (the raw payload
bytes, F0/F7 framing stripped, as a JSON array of ints).

### `action` (a mark: something was dispatched, WITH PROVENANCE)

```json
{"kind": "action", "ts": 1786169888.30, "name": "page.prev", "args": {},
 "origin": "binding:learn_1786169887989"}
```

Stamped **at dispatch time** (not event time) — a coalesced burst of MIDI
can produce its action mark noticeably after the triggering event line,
and replay (§6) relies on exactly this ordering to reproduce observed page
state. `origin` is one of:

| Origin | When |
|---|---|
| `client` | A connected client's own `action` request (CLI/TUI/web). |
| `binding:<id>` | A MIDI binding fired (`bindings.toml`'s `[bindings.<id>]`) — `<id>` is the SAME id `bind list` reports. |
| `behavior` | `PageCycleBehavior`/`ScreensaverBehavior`'s own idle-triggered dispatch. |
| `sysex` | A Cirklon-style SysEx command (`engine/sysex.py`) — bypasses `ActionRegistry.dispatch` entirely, so these three handlers call `CaptureSink.record_action` directly rather than through the dispatch hook the other origins share. |
| `auto` | ONLY ever the very first mark of a session whose `config.capture_auto_start = true` started it — a direct method call at `Engine.__init__` time, bypassing the dispatch hook the same way `sysex` does. |
| `shutdown` | `capture.stop`'s own mark when `Engine.stop()` (a clean daemon shutdown) stops the session, rather than a real client/binding/behavior calling `capture.stop` itself. |

`args` is the action's **fully-coerced** dispatch dict, not merely
whatever the caller happened to supply — `ActionRegistry.dispatch` passes
its `coerced` dict (post type-coercion, post `defaults=` fallback-filling,
see §9) to the post-dispatch hook that feeds this mark, so an OPTIONAL arg
the caller omitted still shows up here at its default value. Concretely:
a `bind.learn` mark for a caller that never passed `--range` records
`"args": {"action": ..., "mode": ..., "args": {...}, "range": ""}` — the
empty-string default, not an absent key — because that's the value the
handler actually ran with.

`capture.stop` is special-cased to record **its own** mark explicitly,
*before* actually stopping (`CaptureSink.stop()` flips `is_recording` false
as its very first observable effect, and `record_action`'s own recording
guard would otherwise silently swallow a stop mark reported through the
normal post-dispatch hook, which only fires *after* the handler returns —
by which point recording has already ended). **This means the very last
line of a cleanly-stopped session's file is always its own `capture.stop`
action mark** — verified live in §8's transcript.

### `page_changed` (the funnel-point record of an absolute page transition)

```json
{"kind": "page_changed", "ts": 1786169888.29, "page": "config"}
```

Emitted from `Engine._set_current_page` — the ONE call site every page
transition funnels through regardless of cause (a `page.goto`/`page.next`/
`page.prev` dispatch, a sysex `CMD_SWITCH_PAGE`, or an idle behavior). This
is what replay applies directly (§6) rather than re-interpreting the
`action` mark that caused it.

### `tempo` (v1's tempo-segment timeline, dedup'd on change)

```json
{"kind": "tempo", "ts": 1786169887.91, "bpm": 135.03}
```

Written whenever a `clock_tick`'s own derived bpm (`60.0 / (ts -
clock_batch_start)`, the same formula `analyzers/transport.py` uses)
differs from the last one recorded. **Informational only** — replay
ignores these entirely (§6): replaying the `clock_tick` events these were
derived FROM already reproduces the identical bpm, so a tempo line carries
no information replay needs that isn't already in the event stream more
completely. Dedup here is simpler than v1's own tick-spacing hysteresis
(`_should_append_tempo_segment`) — v2 has no per-pulse tick to measure
jitter against in the first place, since clock is batched 24-pulses-per-line
(see `engine/midi_in.py`'s own module docstring).

---

## 3. `index.json` — one row per FINISHED session

```json
[{"id": "session-20260807-231807-e3e138aa", "started_ts": 1786169887.77,
  "ended_ts": 1786169891.01, "counts": {"clock_tick": 7, "note_on": 14,
  "note_off": 14, "control_change": 2}, "pinned": false}]
```

Written atomically (tempfile + `os.replace`, same pattern
`engine/bindings.py::BindingsFile.save()` already uses) — safe here
because index writes are cold-path (at most once per `capture.start`/
`.stop`/`.pin`/a write-failure), never per-event like the JSONL body. A
session actively recording has **no** index row yet: it's added only at a
clean `stop()` or at `CaptureSink.fail()` (§4's write-failure path — that
row additionally carries an `"error"` string), so an UNCLEAN shutdown
(a crash/`SIGKILL`, neither of those two code paths) loses the row (see
§4) even though the `.jsonl` file itself may be mostly intact on disk.

A malformed `index.json` is never fatal: `_load_index` catches the parse
failure, logs a warning, and rebuilds a best-effort index by scanning the
sessions directory for `*.jsonl` files and reading each one's own header
line back (`counts`/`ended_ts` can't be recovered this way — reported as
`0`/`None` rather than guessed) — the rebuilt index is immediately
persisted so the recovery sticks.

### Retention

`capture.start` sweeps retention **before** creating the new session (so
the session about to start is never a candidate for its own sweep): every
unpinned row is a candidate, oldest (`started_ts`) first, deleted down to
`retention - 1` — the `-1` is deliberate, not an off-by-one bug: this
call's own about-to-be-created session is the `+1` that brings the
resident unpinned count back up to exactly `retention` right after
`start()` returns (sweeping to the full `retention` here would let the
steady-state count creep to `retention + 1` forever). Default `retention`
is 50 (`Config.capture_retention`). **Pinned sessions are immune outright**
— never counted as excess, never deleted.

### Pin

`capture.pin {id}` sets a completed session's `pinned` flag — only ever
targets a row already in `index.json` (a session that has been STOPPED);
pinning a still-recording session isn't supported (`ActionError`, same
"unknown named resource" precedent as `bind.remove`).

---

## 4. Writer design: the loss window, and write-failure containment

Two *different* failure classes, both disclosed rather than hidden:

**The loss window (unclean shutdown).** Anything appended to the in-memory
deque between the last flush and a hard crash / `SIGKILL` is lost — up to
~1s of events/marks (`flush_interval_s`, default), plus the session's
`index.json` row itself (only ever written at `stop()`). A normal
`SIGTERM`/`SIGINT` shutdown is NOT in this bucket: `Engine.stop()` calls
`CaptureSink.stop()`, which flushes unconditionally first. The raw
`.jsonl` file up through the last successful flush survives either way —
recoverable by hand, just not listed in `index.json` until a clean stop.

**Write-failure containment (disk full / EIO — NOT a crash).**
`flush()`'s own `os.fsync()` can raise `OSError` on a perfectly healthy,
still-running daemon. `Engine._tick_capture_flush` wraps the per-tick
`maybe_flush` call in `try/except OSError` (this used to be unguarded — a
live-reproduced Critical finding: an ENOSPC there propagated straight out
of the whole `run()` tick loop, silently killing it forever with **no log
line at all**, while systemd still reported the unit "active"). A caught
failure routes to `CaptureSink.fail()`: flips the `rec` chrome flag off,
logs exactly ONE error, emits an `alert` event, and **disables capture
outright rather than retrying** — a full disk or read-only filesystem
can't self-heal within seconds the way a vanished USB audio device might,
and retrying at tick rate would spam logs while accomplishing nothing an
operator can't already fix by hand. The same path also catches a
foreground `capture.start`/`.stop` action call whose own I/O raised
`OSError` synchronously, converting it into a clean `ActionError` instead
of tearing down the requesting client connection.

---

## 5. Storage location

Production: `packaging/midicrtd.service`'s `StateDirectory=midicrt`
(systemd creates `/var/lib/midicrt`, owned by the unit's own `User=billie
Group=audio`, persists across reboots) → sessions live under
`/var/lib/midicrt/sessions/`. A bare dev/test run (no systemd) falls back
to `~/.local/state/midicrt/sessions` whenever `/var/lib`'s parent isn't an
existing, writable directory. `Config.capture_dir` (default `None`)
explicitly overrides this resolution.

---

## 6. Replay: `midicrt replay <file> [--speed N | --instant]`

Streams a captured `.jsonl` through a real, **offline** `Engine`
(`engine/replay.py::build_offline_engine` + `stream_session`) — no socket
server, no `MidiInput`, `MidiOutput` stubbed to a silent no-op — producing
a deterministic end-of-replay summary (`events_total`, `events_by_type`,
`actions_by_origin`, `marks_by_kind`, `current_page`, and `final_state`
for voices/harmony/transport/timesig).

### Suppression semantics (the phase's decided design)

Replay **suppresses live binding dispatch, behaviors, and learn arming**
(`Engine(replay=True)`'s gate at the top of `_handle`) — for three
disclosed reasons: double-firing (the marks are already recorded, so
re-dispatching would fire twice), non-determinism (a live binding's
outcome depends on whatever `bindings.toml` happens to be on the
replaying machine, which may have drifted since capture), and
CC-edge-detection baselines (`BindingDispatcher`'s per-binding "last CC
value" state is in-memory and never recorded, so a replayed CC stream
can't reproduce trigger-mode edge crossings faithfully anyway).

**SysEx is the one exception, deliberately.** A captured `sysex` event
line replays through `Engine._handle_sysex` completely normally. Its state
changes are PURE functions of the sysex bytes + the current roster — no
`bindings.toml`/wall-clock dependency to be non-deterministic about — and
its own reply frames land on the stubbed offline `MidiOutput`, so nothing
ever reaches real hardware.

### Mark application semantics — what "apply AS MARKS" means, concretely

- **`page_changed` lines** are applied as a DIRECT state mutation
  (`Engine._set_current_page(mark["page"])`), for every such line
  regardless of which origin produced it live. This is the ONLY way
  replay can reproduce a client/behavior-origin page navigation (both
  have zero MIDI trace) or a binding-origin one (suppressed above) — and
  it's a deliberate refinement over re-interpreting the `action` mark
  that caused it: `page_changed` already IS `_set_current_page`'s own
  absolute-result record, so replay needs no page-roster-order knowledge
  of `page.next`/`page.prev`'s *relative* semantics to reproduce the same
  final answer live dispatch already computed once.
- **Every other `action` mark** is COUNTED (`actions_by_origin[origin]
  += 1`) and **never re-executed** — a deliberate safety property, not
  "not yet implemented": a genuinely re-dispatched `capture.start` would
  try to open a real session file on whatever machine is replaying;
  `bind.learn`/`bind.remove` would mutate a real `bindings.toml`;
  `sendnotes.key` would (absent the output stub) send a real note. There
  is no safe, general way to replay an arbitrary action mark as a real
  dispatch without auditing every current AND future action one at a
  time — mark-counting has no such risk, since it's a pure read of the
  mark's own `origin` field.
- **`tempo` lines**: ignored entirely (see §2 — no information replay
  needs that isn't already in the `clock_tick` events).
- **Any other/future mark `kind`**: silently ignored (forward-compatible
  — a plain `if/elif` chain with no `else: raise`).

### Timing model

Every `MidiEvent` built from an "event" line carries its ORIGINALLY
RECORDED `ts` verbatim in every mode — `--instant` only skips the
between-event `time.sleep()` calls, it never rewrites a `ts` value (bpm/
timesig derivations read `ev.ts` directly, so substituting wall-clock
"now" would corrupt them and make two replays of the same file produce
different answers). `--speed N` paces `time.sleep(max(0, ts - prev_ts) /
speed)` between consecutive event lines; action/page_changed/tempo lines
apply instantaneously with no pacing contribution of their own.

### Disclosed limitation: tick-driven state is FROZEN during replay

`stream_session` never calls `Engine.run()` — it feeds event lines
straight into `Engine._handle` and applies marks directly. That means
`_tick_analyzers`/`_tick_pages`/`_tick_behaviors` (the `run()` loop's own
once-per-tick hooks) never run, for ANY wall-clock value, real or
log-derived. Concretely: `analyzers/stucknotes.py`'s long-held-note
escalation (fires from `tick(now)` alone, no new MIDI event required)
never escalates during replay, however long a note in the log stays held;
`pages/pianoroll.py`'s wall-clock scroll never advances either. Currently
harmless for every field `_build_summary` actually reports (voices/
harmony/transport/timesig are all pure `handle(ev)` state machines) — but
a real fidelity gap, not silently assumed away. Fixing it (driving ticks
from the log's own `ts` progression) is flagged as a seam for a future
task, not attempted here.

### Disclosed nuance found during this task's own live smoke: `transport.running` needs a captured `start`/`continue`, not just clock

`analyzers/transport.py::TransportAnalyzer._running` starts `False` and
only ever becomes `True` on an explicit MIDI `"start"`/`"continue"`
message — a bare `clock_tick` while not running is ignored outright (this
is intentional v1-matching behavior, not a replay bug: see that module's
own docstring). A **live** daemon's transport is almost always already
running by the time anyone starts a capture (the sequencer/DAW sent its
one `"start"` message long before, possibly hours earlier, well outside
the capture window) — so a capture that never happens to include a
`"start"`/`"continue"` line will faithfully replay with `running: false,
bpm: null` in `final_state.transport`, even though the LIVE session was
clearly running with a real bpm throughout. This is the CORRECT,
deterministic answer for what's actually IN the file (verified live: §8's
own transcript shows exactly this), not a defect — but it means
`final_state.transport` is not a reliable "was the sequencer running"
check unless you know the capture window happened to span a transport
start. Worth knowing before treating a `running: false` replay result as
"the source wasn't playing."

---

## 7. Learned-binding port durability (this task's fix)

**The problem.** A real ALSA/RtMidi `MidiEvent.source` string always ends
in a volatile `" <client>:<port>"` ALSA sequencer-numbering suffix (e.g.
`"Midi Through:Midi Through Port-0 14:0"`) — those two integers are
assigned by ALSA at connect time, in enumeration order, and reliably
renumber across a reboot, a USB replug, or (now that this Pi's LAN net-MIDI
runs on rtpmidid) an rtpmidid session restart. `bind.learn`'s capture used
to write `ev.source` into `BindingMatch.port_pattern` completely VERBATIM
— an exact-string match against a suffix near-guaranteed to be a different
number the next boot, silently turning every learned binding into a
landmine that stops firing the first time its physical port re-enumerates.

**The fix.** `engine/bindings.py::glob_port_pattern(source)` strips the
trailing `" NN:M"` suffix and appends a trailing `"*"`, escaping any
`fnmatch` special character (`*`/`?`/`[`) in the surviving literal portion
via `glob.escape` (glob and `fnmatch` share the exact same special-character
set, so this is a correct escaper for both, no new dependency).
`"Midi Through:Midi Through Port-0 14:0"` becomes
`"Midi Through:Midi Through Port-0*"` — still matches the exact string it
was learned from (`'*'` matches the stripped suffix, space included) AND
matches the same port re-enumerated as `"...Port-0 23:1"` after a reboot.
A source with no trailing `"NN:M"` suffix at all (never observed against
real hardware, but not something this pure string function can assume of
every future MIDI backend) falls back to an exact, non-globbed, escaped
pattern — behaviorally identical to the old verbatim behavior for that
input shape.

`Engine._capture_learn` is the one production call site
(`engine/core.py`) — every OTHER binding-matching code path
(`BindingDispatcher._matches`) is untouched, since `fnmatch.fnmatch`
already treats an exact string and a glob identically.

### `bind.list` gains `port_present: bool`

Pure diagnostic sugar, computed fresh on every `bind.list` call: is
`match.port_pattern` currently matched by ANY open MIDI input port?
`Engine.set_open_ports_provider` (mirrors the pre-existing
`set_topic_refcount_provider` PULL-seam shape exactly) is wired once, in
`daemon.py::build()`, to `MidiInput.open_ports` — read LIVE on every call,
not a snapshot frozen at wiring time. `port_pattern is None` ("any port")
is trivially always present; no provider wired (`--no-midi`, or the
overwhelming majority of this test suite's bare `Engine()` instances)
means "unknown", reported `True` rather than spuriously flagging a
binding as port-missing just because nothing ever told the engine what's
open. Never gates whether a binding actually fires — `BindingDispatcher`
is completely untouched by this field.

### Disclosed limitation: identical-device collision (the tradeoff this fix accepts) — **fixed for serial-bearing devices, Phase 9 Task 1**

Stripping the trailing `<client>:<port>` suffix buys durability across a
reboot/replug/rtpmidid restart (§7 above) at a real cost: it also strips
the ONE thing that could have distinguished two DIFFERENT physical ports
that happen to share the same ALSA-enumerated NAME — most commonly two
simultaneously-connected units of the same USB MIDI interface model (ALSA
names both, e.g., `"USB MIDI Interface:USB MIDI Interface MIDI 1"`,
distinguished ONLY by the client:port numbers this fix throws away). A
binding learned against "device A" globs to a pattern that ALSO matches
"device B" — so a control on the second, physically distinct unit will
silently ALSO fire that binding, with no error and no warning. `bind.
list`'s `port_present: true` does not catch this either: it only checks
whether SOME open port matches, not whether EXACTLY one does, so a
collision reads as perfectly healthy.

This was a deliberate, accepted tradeoff, not an oversight, when this
section was first written: the failure mode this task fixed (every
learned binding going permanently dead on the very next reboot) was both
more common and worse than the failure mode disclosed here (a binding
firing from an unintended second identical-model device, which requires
two units of the exact same hardware connected at once to even be
POSSIBLE). It has since been **closed for the common case**: see
`engine/midi_identity.py` (Phase 9 Task 1, .superpowers/sdd/
2026-08-09-midicrt2-phase9-instruments/task-1-brief.md) and
`docs/phase4-bindings.md`'s `device` field row. In short — `bind.learn`
now ALSO resolves and captures a stable `usb:<vendor>:<product>:<serial>`
/ `virt:<name>` device identity (`BindingMatch.device`), which becomes the
SOLE match key whenever present (port_pattern is retained on disk as an
inert fallback, not consulted). For any USB MIDI interface that exposes a
hardware serial number, this fully resolves the collision this section
originally disclosed: two distinct-serial units of the same model no
longer cross-fire each other's bindings, and a binding correctly follows
its device across a port move.

**What's still open, disclosed honestly rather than silently traded
away:** a USB MIDI interface with NO hardware serial number (confirmed to
exist on real, cheap hardware — this Pi's own attached USB Audio Device
has no `serial` sysfs attribute at all; see `IdentityResolver`'s own
module docstring for the live probe) resolves to `usb:<vendor>:<product>`
with no distinguishing third segment — two simultaneously-connected units
of that exact serial-less model are STILL indistinguishable and will
still cross-fire, exactly as before this task. `bind.list`'s
`device_present: true` has the identical "checks *some* match, not
*exactly one*" limitation `port_present` always had. If you rely on
multiple identical-model, serial-less MIDI interfaces plugged in
simultaneously, hand-edit the affected binding's `port_pattern` (or
`device`) in `bindings.toml` back to something that disambiguates them —
see `docs/phase4-bindings.md`'s own `device`/`port_pattern` rows for the
hand-edit convention. The "Phase-7 follow-up idea" below (a match COUNT,
not just a boolean) remains unbuilt and would help here too, now for
`device_present` as much as `port_present`.

**Phase-7 follow-up idea** (not built, flagged for later): `bind.list`
could additionally report how MANY currently-open ports/devices match a
binding's `port_pattern`/`device` (not just whether ≥1 does), so a human
diagnosing "why is my binding firing from the wrong device" could see a
count of 2+ and immediately suspect this exact collision, rather than
`port_present`/`device_present: true` reading as unconditionally
reassuring.

---

## 8. Live smoke evidence (on-Pi, real production `midicrtd`, real hardware)

Full annotated transcript: on motherbase,
`~/projects/pivisualizer/.superpowers/sdd/2026-08-07-midicrt2-phase5-capture/task-3-report.md`
(this task's SDD ledger).
Summary of what was proven, live, against the running daemon with real
MIDI hardware/ambient traffic (a live sequencer was genuinely running on
this Pi throughout the test — unplanned but load-bearing evidence, same
spirit as Tasks 1/2's own live smokes):

- `capture.start` → `bind.learn {page.prev, trigger}` → a real note (65)
  injected via "Midi Through" captured the learn, producing a binding with
  `port_pattern: "Midi Through:Midi Through Port-0*"` (the durable,
  suffix-globbed form — proving §7 live) and `port_present: true`.
- The SAME note injected again fired the binding for real (`page.prev`,
  origin `binding:learn_<id>`) — observed via `midicrt status`'s
  `page` field changing.
- A `midicrt-fb --out` frame taken WHILE still recording showed the `●
  REC` chrome marker in the status bar (`clients/chrome.py::REC_MARKER`).
- `capture.stop`'s own `counts` (`{"clock_tick": 7, "note_on": 14,
  "note_off": 14, "control_change": 2}`, summing to 37) matched
  `midicrt replay --instant`'s `events_total`/`events_by_type` **exactly**
  — byte-for-byte, not approximately.
- `actions_by_origin` from replay (`{"client": 4, "binding:learn_...": 1}`)
  exactly reproduced the file's 5 action marks, INCLUDING the
  binding-origin one — and the raw file's last line is that session's own
  `capture.stop` action mark (§2's "capture.stop is special-cased" claim,
  confirmed against a real file, not just a unit test).
- `final_state.voices.total_peak: 8` was hand-traced against the raw
  file's own note_on/note_off lines (a real ambient note overlapping with
  a synthetic 3-note chord, each delivered via two ALSA paths due to the
  test harness's own loopback port being simultaneously discoverable as
  an input — ALSA's own bidirectional-port behavior, not a capture bug)
  and matched replay's reported peak exactly.
- The learned binding was removed (`bind.remove`) and all scratch session
  files + `index.json` deleted immediately after; `midicrtd` was restarted
  afterward and verified pristine (`capture.status` idle,
  `last_session: null`, sessions directory empty, `bind list` empty, 21
  actions unchanged).

---

## 9. CLI `--range lo,hi` for continuous learn

`midicrt bind learn <action> --mode continuous --range lo,hi` overrides
the default `[0.0, 1.0]` lerp target — e.g. `--range 0.25,4.0` for a
zoom-style parameter whose useful range doesn't start at 0 (previously
every continuous learn was stuck at `[0, 1]`, so a learned zoom knob only
ever reached the bottom quarter of `pianoroll.zoom_level`'s actual
`[0.25, 4.0]` range). Silently ignored for `--mode trigger` (no `range`
concept there). Wire-level: `bind.learn` gained a genuinely OPTIONAL
`range: str` arg — `ActionRegistry.register`'s new `defaults=` parameter
(`engine/actions.py`) is the general mechanism this uses: a schema arg the
caller omits falls back to an already-coerced default (here, `""`,
meaning "not supplied") instead of a hard "missing arg" error, so every
pre-existing `bind.learn` call site (none of which pass `range`) keeps
working unchanged. Full schema reference: `docs/phase4-bindings.md` §4.

---

## 10. Help page renders the live keymap

The `help` page's `view_model()` now includes `keymap_rows` — one row per
`keymap` entry (key → action, sorted by key), rendered as a third
`-- Keymap --` section after Pages/Actions in both the TUI (`clients/
tui.py::_help_body_lines`) and framebuffer (`clients/fb/app.py::
render_help_frame`) clients. Sourced from `Engine.keymap` (read live, not
frozen at wiring time — a `config.reload` that changes the keymap shows up
on the help page's very next render with no extra plumbing). Verified live
against the real daemon via `midicrt-fb --out`: shows the shipped
`DEFAULT_KEYMAP` (`c: eventlog.clear`, `n: page.next`, `q: client.quit`).

---

## 11. Troubleshooting

**A session file exists but has no `index.json` row.** The daemon crashed
or was `SIGKILL`ed before a clean `capture.stop` — see §4's loss-window
disclosure. The `.jsonl` file itself (up through the last flush) is
recoverable by hand; `capture.status`'s `last_session` won't reflect it
until the NEXT time any session stops cleanly (which triggers a fresh
`index.json` read/rebuild).

**Replay's `final_state.transport.running` is `false` but I know the
source was playing.** See §6's dedicated nuance — the capture window
almost certainly didn't happen to include a `"start"`/`"continue"`
message; this is expected, not a bug.

**A learned binding stopped firing after a reboot/replug.** Should no
longer happen for anything learned after this task (§7) — `bind list`'s
`port_present: false` on an entry is the fastest way to confirm a port
genuinely isn't currently open (vs. some other mismatch). A binding
learned BEFORE this task (hand-edited `bindings.toml`, or captured by a
pre-Task-3 build) still has the OLD exact-string pattern — re-learn it, or
hand-edit its `port_pattern` to a glob per `docs/phase4-bindings.md`.

**Capture silently stopped recording with an `alert` event.** Check
`journalctl -u midicrtd` for a `capture: write failed` line (§4) — disk
full or a read-only filesystem, most likely. Capture does NOT auto-retry;
fix the underlying storage issue and issue a fresh `capture.start`.
