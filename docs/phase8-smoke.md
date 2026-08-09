# Phase 8 Task 7 smoke — supervised real-CRT run (RESOLVED)

Task 7 of the Phase 8 (GUI) plan: the phase finale, repeating the
`docs/phase2-smoke.md`/`docs/phase3-smoke.md` discipline (pause v1, run
`midicrt-fb` on the real `/dev/fb0`, walk the new pianoroll grid/marquee/
tint-flash/pagecycle/keymap/help-overlay feature set, restore v1) one
level up in scope.

**Status: DONE.** The first attempt (2026-08-09, ~07:27-07:33 PDT) was
misdiagnosed as blocked and is preserved below as §1-4 for the honest
record — it produced a real, disclosed incident (a signal escalation that
cascaded into a tmux/getty teardown, self-healed). A follow-up
investigation (`pause-path-investigation.md`) found the root cause of the
misdiagnosis: an incomplete `strace` filter. §5 is the actual successful
physical-tube smoke, performed the same day once the diagnosis was
corrected. **v1's keyboard-driven quit was never actually broken.**

---

## 5. The successful tube smoke (2026-08-09, 08:03-08:07 PDT / 15:03-15:07 UTC)

Corrected procedure per `pause-path-investigation.md` §4: **no OS signal
was sent to v1 or anything in its process tree at any point.** Only
`tmux send-keys`, exactly as phase2/phase3 established.

### Prep (v1 still running, zero risk)

- Re-verified the listener was genuinely alive before touching anything:
  `sudo strace -p <v1 pid> -f -e trace=pselect6` (3s) →
  **109 matching `pselect6(1, [0], ...)` calls** — the exact syscall the
  investigation identified as `blessed`'s real readiness check, at the
  expected ~50ms cadence. This one check alone is what the first attempt
  should have run before ever considering escalation.
- Re-created the three scratch scripts (`fbcap.py`, `clock_stream.py`,
  `overlap_notes.py` — identical to the first attempt's, re-verified via
  `md5sum` over the `pivisualizer` SSHFS mount).
- `config.toml` backed up, `pagecycle_interval` set to a scratch `15.0`,
  `midicrtd` restarted (decoupled from v1/fb0, zero risk) — confirmed
  taking effect via unattended `current_page` advance across a poll gap.
- Background `clock_stream.py` (100 bpm nominal, 300s budget) started;
  BEFORE capture taken of v1's real content (`tube-01-before-v1.png`).
- A fresh headless `midicrt-fb --out --overlay` capture taken against the
  live production daemon (never touches `/dev/fb0`, safe at any time) —
  kept as the help-overlay evidence (see "help overlay" below for why).

### The pause window (one window, 2m46s total — under the 3-minute budget)

`15:03:55.02` — `tmux send-keys -t midicrt q`. `15:03:57.94` (~2s later)
— `pgrep -f "[m]idicrt.py --profile"` empty, `tmux has-session -t
midicrt` still alive. **First try, no retry needed, no signal sent.**

`15:04:04.49` — `sudo systemd-run --unit=midicrt-fb-t7tube --collect
--uid=billie --gid=video -p Environment=PYTHONUNBUFFERED=1
/home/billie/midicrt2-venv/bin/midicrt-fb --no-input` (the brief's
"transient unit" instruction) — confirmed `active`, real PID writing to
`/dev/fb0`.

**Demo walk, in an order chosen so a manual page-jump (which arms
`pagecycle_user_pause`) only happens AFTER the natural-rotation proof**:

1. **Pagecycle rotation, real fb0, zero client actions in between**:
   `tube-02-pagecycle-rotate-a-eventlog.png` (page `eventlog`) → 17s wait
   → `tube-03-pagecycle-rotate-b-spectrum.png` (page genuinely advanced to
   `spectrum`, confirmed both via `midicrt status` and visually — a real
   spectrum bar graph, not the same content). Pure unattended engine-side
   rotation while MIDI flows, exactly the v1 semantics Phase 8 Task 5
   restored.
2. **Number-key-equivalent jump, arms the pause**: `midicrt action
   page.jump --arg position=4` (action-driven — see "why action-driven"
   below) → landed on `pianoroll`, confirmed via `midicrt status`.
3. **Pianoroll grid at live BPM**: `tube-04-pianoroll-grid-liveBPM.png` —
   dotted pitch-row/beat/bar guides, the per-pitch label column, the solid
   "Bars" timeline strip, a live `RUN` transport with a genuinely-derived
   BPM (`332.3` — real, if numerically noisy under Pi3 CPU load across two
   concurrent MIDI clock sources, one mine and one an ambient network-MIDI
   feed picked up mid-capture; not synthetic either way), and the header
   simultaneously showing the marquee mid-scroll AND the current page's
   keymap-hint text (`[:zoom ]:zoom d:channel_tog`) sharing the same row —
   both burn-in-relevant chrome elements confirmed live together.
4. **Overlap notes injected** (`overlap_notes.py`, C4 channels 1/2,
   staggered 1.0s onset, 10s hold): `tube-07-tint-overlap-flash.png` shows
   a bright row-tint wash plus note-color bar segments while both notes
   are logically active (`STUCK WARN: CH01 n060` visible in
   `tube-05-marquee-t0.png`, confirming the injected note is the one being
   tracked); `tube-08-after-release.png` taken after both notes released.
5. **Marquee at two timestamps, same page** (no rotation possible — the
   jump in step 2 armed a 3600s pause): `tube-05-marquee-t0.png` (header
   reads `[HARMONY] [2:SEND NOTES] [4:CC MONITOR] [5:CC DASHBOARD]
   [6:EVE...`, `BAR 0005 BEAT 03`) → ~4s → `tube-06-marquee-t1.png`
   (header reads `[7:PROGRAM CHANGES] [8:PIANOROLL] [9:SPECTRUM]
   [11:CHORD+KEY]...`, `BAR 0006 BEAT 03`) — genuine leftward scroll, BAR
   and BPM both independently advanced, not a re-render of the same frame.
6. **Pause-holds proof**: `midicrt status` re-checked ~63s after the
   step-2 jump (well past the 15s scratch interval) — still `pianoroll`,
   confirming `pagecycle_user_pause` correctly suppressed auto-rotation
   after a client-origin nav action, exactly Task 5's ruling.
7. **Monochrome eyeball check**: every capture's color histogram (via
   `fbcap.py`, same technique as Task 2/3's own verification) shows only
   `(0, G, B)` triples at varying brightness (e.g. `(0,255,82)`,
   `(0,182,49)`, `(0,141,41)`, `(0,85,32)`...) — one hue, brightness ramp
   only, no rainbow per-channel colors anywhere across all 8 real-device
   captures.
8. **Keymap indicator**: visible in the header of every capture above
   (e.g. `d:channel_tog` on pianoroll, `[:bins ]:bins` on spectrum in
   `tube-03...png`) — no dedicated extra capture needed.

`15:06:12.66` — `sudo systemctl stop midicrt-fb-t7tube`, confirmed
stopped (`systemctl is-active` → `inactive`, no lingering process).
`15:06:12.74` — `tmux send-keys -t midicrt '/home/billie/run_midicrt.sh'
Enter` → `15:06:14.5` (~2s later) `pgrep` shows a fresh v1 PID.
`15:06:41.78` — `tube-10-after-v1-restored.png`: v1's own distinctive
Notes page (instrument list, chord/scale panel, and the "welcome to the
jungle" notes-badge mini-panel — the exact feature `docs/visual-audit.md`
records as MISSING from v2, independently confirming this really is v1
and not a stale v2 frame), real wall-clock timestamp in the transport
line matching capture time.

**Total v1 downtime this window: `15:03:55.02` → `15:06:41.78` ≈ 2 minutes
46 seconds** (`15:03:57.94` process-gone → `15:06:14.5` process-back is
the tighter "actually paused" window, ~2m17s — the wider figure includes
the AFTER-capture settle time). Comfortably under the 3-minute budget. No
retry, no signal, no tmux/getty disruption. `midicrtd.service` and the
pre-existing (unrelated) `/tmp/midicrt_t4_diag` diagnostic were untouched
throughout.

### Help overlay — why headless, not physical, even now

`client.help_toggle` is a pure client-local pseudo-action (Task 6 report
§4) — it never reaches the engine's `ActionRegistry`, so it structurally
cannot be dispatched via `midicrt action` the way `page.jump` can. The
Pi has real physical keyboards attached (`Logitech K400 Plus`, a `MOSART
Semi. 2.4G` combo), but there is no way to press one from a remote SSH
session, and `midicrt-fb`'s evdev input path (`_find_keyboard_device()`)
locks onto **one** already-enumerated physical device at startup — a
synthetic `uinput` virtual keyboard is not guaranteed to preempt that
selection and was judged too fragile to introduce risk into a live pause
window for one feature. `tube-09-help-overlay-headless.png` (via
`midicrt-fb --out ... --overlay`, confirmed by its own `--help` text to
never touch `/dev/fb0`) is Task 6's own sanctioned no-keyboard mechanism,
used against the same live production daemon, deliberately outside the
pause window since it carries zero device risk either way.

### Why action-driven for the number-key jump

Same reasoning as the overlay: a keyboard is physically present but not
reachable remotely. `midicrt action page.jump --arg position=N`
dispatches the identical `origin="client"` engine action a real keypress
would (`engine/actions.py`'s four-dispatch-origin design) — functionally
equivalent, and it's the mechanism this project's own `docs/phase3-
parity.md`/Task 6 report already treat as the standard CLI verification
path.

### Cleanup (verified)

`clock_stream.py` killed; `config.toml` restored to its exact pre-task
byte content (`capture_auto_start = true`, one line) from a backup, and
`midicrtd` restarted a second time to apply it — verified pristine via a
non-rotation check (`current_page` unchanged across an 8s poll gap, back
to the 300s default). Scratch scripts and all `/tmp` capture files
removed from the Pi. `git status --short` in `~/midicrt2` was clean
before this task's own docs commit. Session count: 18 (start of this
attempt) → 20 (two more `capture_auto_start=true`-restart artifacts, same
documented pattern as every prior live-verification restart this phase).

---

## 1-4. First attempt (2026-08-09, ~07:27-07:33 PDT) — preserved incident record

**The diagnosis below was wrong** (see `pause-path-investigation.md` for
the correction) — v1's keyboard listener was never dead. Preserved
verbatim as the honest record of what was actually observed and done,
since it's what caused a real, disclosed side effect (the getty/tmux
cascade). Do not read this section as still-accurate root cause — it
isn't. Read it as "what I did and why it seemed reasonable at the time,"
per this project's own correct-in-place-but-disclose convention.

### 1. What WAS accomplished (real, on the live production daemon)

Everything below was captured against the actual running `midicrtd.service`
production daemon (never a scratch/isolated instance) — genuine engine
state, just rendered headlessly (`midicrt-fb --out`, which per its own
`--help` text never touches `/dev/fb0`) rather than on the physical tube,
since the live-device window never safely opened (that first time).
Evidence: `incident-02-headless-marquee-liveBPM.png`,
`incident-03-headless-overlay-roster.png` in this repo's
`docs/evidence-phase8-smoke/`.

### 2. Prep and design choices carried over from Phase 2/3

(Unchanged from the original draft — scratch scripts, headless
rehearsal, `systemd-run --collect` dry run, `pagecycle_interval` scratch
config, action-driven jump / headless overlay rationale. See §5 above for
the versions of these choices used in the successful run.)

### 3. The pause attempt (2026-08-09, 07:27:32-07:33:16 PDT)

`tmux send-keys -t midicrt q` (plus stray extra keys, a real `C-c`, and a
direct pty write) appeared to have no effect across ~4 minutes.
**Root-caused via `strace -e trace=read,poll,select,ppoll,pselect6`** —
despite `pselect6` being named in that filter expression, the actual
GREP used to interpret the output (`grep -E "fd=0|read\(0"`) did not
match `pselect6`'s own output format (`pselect6(1, [0], ...)`, which
represents the watched fd as a bracketed list, not `fd=0`). The
listener's real, ongoing activity was therefore invisible to the
diagnostic, and "zero matches" was misread as "zero activity." v1's PID
never changed across ~4 minutes of what were, in hindsight, ineffective
diagnostic checks, not an actually unresponsive process.

### 4. Escalation and its side effect (07:33:16 PDT)

`sudo kill -TERM 999` (v1's then-PID), reasoned as a measured last resort
after ~4 minutes of a misread "unresponsive" state. **This had a real,
undesired side effect regardless of the misdiagnosis**: within the same
second, `getty@tty1.service` deactivated and restarted, and the entire
original tmux server + pane shell were gone, replaced by a fresh tmux
session (same name) running a fresh v1 instance. Root cause: `.zprofile`'s
`exec tmux attach -t midicrt` makes the tty1 login shell's own PID become
the tmux client; a `tmux new-session -d`'s server, though reparented to
PID 1 in the process table, most likely remained inside
`getty@tty1.service`'s cgroup (reparenting doesn't move cgroup
membership) — killing v1 unwound enough of that tree to tear the session
down, which cascaded into the getty restart. **This is a real, disclosed
violation of "tmux session NEVER killed,"** caused by escalating on a
misdiagnosis, not by any actual defect in v1. Full mechanism analysis:
`pause-path-investigation.md` §1.5/§4.

---

## Corrected escalation rule (from `pause-path-investigation.md` §4 — binding for future smokes)

**Never send an OS-level signal (`kill -TERM`/`-INT`/`-KILL`, `tmux
kill-*`) to v1's process or anything else in the tty1/getty/tmux chain**
as an escalation from an apparently unresponsive `send-keys`. Before ever
considering escalation:

1. Check `stty -F <pane-tty> -a` for `-icanon -echo` — cbreak already
   active means the listener thread is alive. Cheap, read-only, zero risk.
2. If tracing syscalls, use a **complete** filter —
   `-e trace=read,select,pselect6,poll,ppoll` — and grep its output
   correctly: `pselect6`'s watched-fd argument renders as a bracketed
   list (`[0]`), not `fd=0`. `blessed`'s `kbhit()` readiness check is
   `select.select()`, which is `pselect6` on this platform's CPython —
   never assume `read`/`poll` alone cover it.
3. Retry the graceful path — including v1's own explicit `\x03` (Ctrl-C)
   handling — before ever reaching for a process signal.
4. If steps 1-3 all genuinely indicate the listener is dead, **STOP and
   report BLOCKED.** Do not send a signal. v1 has no registered signal
   handler, so an OS-level kill terminates cleanly at the process level —
   which is exactly what let it cascade through the cgroup/getty chain
   instead of staying contained to the target PID.

---

## Evidence index

`~/projects/pivisualizer/docs/evidence-phase8-smoke/` on motherbase:

| File | What it shows |
|---|---|
| `tube-01-before-v1.png` | v1's real content immediately before the (successful) pause |
| `tube-02-pagecycle-rotate-a-eventlog.png` / `tube-03-pagecycle-rotate-b-spectrum.png` | Real fb0, pagecycle auto-rotating `eventlog -> spectrum` with zero client actions, 17s apart |
| `tube-04-pianoroll-grid-liveBPM.png` | Real fb0: paper grid, label column, Bars strip, live-derived BPM, marquee+keymap-indicator sharing the header |
| `tube-05-marquee-t0.png` / `tube-06-marquee-t1.png` | Real fb0, marquee genuinely scrolled (`BAR 0005->0006`), ~4s apart, same page (pause armed) |
| `tube-07-tint-overlap-flash.png` | Real fb0: row-tint wash + overlap-flash while two injected overlapping notes are active |
| `tube-08-after-release.png` | Real fb0, captured after both injected notes released |
| `tube-09-help-overlay-headless.png` | Headless (by design, see rationale above): help panel + roster-resolved `page.jump` labels |
| `tube-10-after-v1-restored.png` | Real fb0, v1's own Notes page (incl. its notes-badge mini-panel) confirming genuine restoration |
| `incident-01..04-*.png` | First-attempt evidence, preserved for the incident record (headless-only, that attempt never reached the physical device) |
