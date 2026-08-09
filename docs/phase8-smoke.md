# Phase 8 Task 7 smoke — supervised real-CRT run (BLOCKED, incident report)

Task 7 of the Phase 8 (GUI) plan: the phase finale, meant to repeat the
`docs/phase2-smoke.md`/`docs/phase3-smoke.md` discipline (pause v1, run
`midicrt-fb` on the real `/dev/fb0`, walk the new pianoroll grid/marquee/
tint-flash/pagecycle/keymap/help-overlay feature set, restore v1) one
level up in scope. **The live-tube pause portion did not complete.** v1's
graceful keyboard-quit path is currently unresponsive on this deployment
(reproduced on two separate process instances, see §3), and one escalation
attempt to recover from that had a real, undesired side effect (§4). This
doc is the honest incident record, not a success report — read it before
attempting another supervised pause of v1 on this box.

## 1. What WAS accomplished (real, on the live production daemon)

Everything below was captured against the actual running `midicrtd.service`
production daemon (never a scratch/isolated instance) — genuine engine
state, just rendered headlessly (`midicrt-fb --out`, which per its own
`--help` text never touches `/dev/fb0`) rather than on the physical tube,
since the live-device window never safely opened. Evidence copied to
`~/projects/pivisualizer/docs/evidence-phase8-smoke/` on motherbase:

- **`headless-eventlog-marquee-liveBPM.png`** — real production state:
  header title bar mid-scroll (`"[CES] [14:CONFIG] [17:IMG2TXT] [0:HELP]
  [1:HARMONY] [2:SEND NOTES] [4:CC MONITOR] [5:CC DA"`, the marquee
  proven live-scrolling in Phase 8 Task 4), a real derived BPM (`132.0
  BPM RUN`, sourced from a genuine 24-ppqn MIDI clock stream injected via
  `mido` on `Midi Through:Midi Through Port-0`, same technique Tasks 3/4
  used), a `STUCK WARN` chrome row, and the eventlog page's real event
  stream (`note_on`/`note_off`/`start`/`stop` lines from the injection
  scripts below). Confirms: marquee mechanism, live-clock-derived BPM
  status row, and monochrome rendering (`distinct_nonblack_colors=1`,
  `(0,255,82)` only — verified via the raw-fb-style color histogram in
  `fbcap.py`, reused from Task 2/3's own verification method) all hold on
  the real engine.
- **`headless-help-overlay-roster-resolved.png`** — `midicrt-fb --out
  ... --overlay` (Phase 8 Task 6's own sanctioned no-keyboard verification
  mechanism — this project's precedent for proving the overlay renders
  without a physical key event, see `task-6-report.md` §4/§6) against the
  SAME live daemon: the dim `LUM_FAINT`-backdrop GLOBAL panel, with
  `page.jump` entries correctly resolved against the real 14-page roster
  (`"1 -> eventlog"`, `"2 -> voices"`, `"3 -> harmony"`, `"4 ->
  pianoroll"`, ... through `"9 -> help"`, `"@ -> ccdashboard"`) and
  genuinely out-of-range positions (this roster has 14 pages) correctly
  still showing the bare `"page.jump"` fallback (`%`, `^`, `&`, `(`, `)`,
  `*`). Also proves the on-screen keymap indicator/hint mechanism is live
  (visible in the header of both captures) without needing a dedicated
  third screenshot.
- **CLI action-driven page-jump** (the brief's own "USB keyboard if
  attached else action-driven" fallback — see §2 for why action-driven
  was chosen even though a keyboard IS physically present): `midicrt
  action page.jump --arg position=4` landed on `pianoroll` (verified via
  `midicrt status`); `position=8` landed on `config` — both match the
  roster order `eventlog, voices, harmony, pianoroll, spectrum,
  screensaver, img2txtviz, config, ...` from `config.py`'s `pages`
  default list, independently confirming Task 6's own worked example
  (`position=4 -> pianoroll`).
- **Pagecycle scratch-interval rotation** (production `config.toml`
  temporarily set to `pagecycle_interval = 15.0`, `midicrtd` restarted to
  apply it — a change fully decoupled from v1/fb0, see §2): `midicrt
  status` polled ~17s apart showed `current_page` genuinely advance
  `eventlog -> harmony` with no client action in between, proving the
  v1-semantics rotation (Phase 8 Task 5) still auto-advances while MIDI
  flows, independent of whether any client is attached to render it.
- **Overlap-notes injection script tested** against the live daemon (a
  2s-hold dry run) — confirmed `events_total` advanced and no errors;
  the full-hold (12s) version was never run against the real fb0 client
  because the pause window never opened.

None of this touched `/dev/fb0` or v1. Scratch config was fully restored
(`config.toml` back to its pre-task single line, `capture_auto_start =
true`; `midicrtd` restarted a second time to apply it — verified pristine
by a repeat `pagecycle_interval` non-rotation check, §6).

## 2. Prep and design choices carried over from Phase 2/3

- Capture tool: a small raw-`/dev/fb0`-to-PNG script
  (`fbcap.py`, ~30 lines), same technique/geometry as phase2/3's own
  throwaway `fbcap.py` (800x475, stride 1600, 16bpp RGB565, confirmed
  fresh via `/sys/class/graphics/fb0/*` before use, not assumed) — never
  writes to the device.
- MIDI injection: real `mido` messages on `Midi Through:Midi Through
  Port-0 14:0` (the same permanent ALSA port `/tmp/inject_midi2.py` from
  an earlier session already used) — a real clock-pulse stream
  (`clock_stream.py`, `start` + 24-ppqn `clock` pulses + `stop`) and a
  real overlapping-note injector (`overlap_notes.py`, C4 on channels 1/2
  staggered 1.0s, matching Task 4's own live-evidence technique). Both
  tested against the ALREADY-RUNNING production daemon before ever
  touching v1 — harmless (v1 sees the same benign traffic too, exactly
  like Task 3/4/5's own "keep last_activity_ts fresh" precedent).
- **Page-jump demonstration chosen as action-driven, not physical
  keyboard, even though a keyboard IS attached** (`Logitech K400 Plus` +
  a `MOSART Semi. 2.4G` wireless keyboard/mouse, confirmed present via
  `/proc/bus/input/devices`): there is no way to physically press a key
  on that keyboard from a remote SSH session, and `midicrt action
  page.jump --arg position=N` dispatches the IDENTICAL engine-side action
  a real keypress would (`origin="client"`, per `engine/actions.py`'s
  four-dispatch-origin design) — functionally equivalent, lower-risk than
  attempting synthetic `uinput` key injection for a supervised safety
  procedure.
- **Help overlay chosen via the headless `--out --overlay` flag, not a
  live keypress**: `client.help_toggle` is a pure client-local
  pseudo-action (Task 6 report §4) that never reaches the engine's
  `ActionRegistry` at all — it structurally CANNOT be dispatched via
  `midicrt action`. The only two ways to show it are a real keypress (no
  physical access, as above) or the `--overlay` flag Task 6 itself built
  and documented as "the brief's own suggested mechanism for proving the
  panel renders correctly with no interactive keyboard on the Pi." Used
  that precedent directly.
- Rehearsal discipline: BOTH headless captures above, plus the
  `pagecycle_interval` scratch-config restart cycle, were fully rehearsed
  and verified BEFORE ever sending a pause keystroke to v1 — per the
  brief's "prep EVERYTHING first" instruction. `systemd-run --collect`
  (the brief's "transient unit" instruction) was dry-run tested
  (`t7-dryrun.service`, uid/gid resolved correctly, auto-collected on
  exit) before being staged for the real `midicrt-fb` launch, which never
  happened.

## 3. The pause attempt (2026-08-09, ~07:27-07:33 PDT / 14:27-14:33 UTC)

Preconditions verified identically to phase2/phase3 (`tmux has-session -t
midicrt` alive, `pgrep -f "[m]idicrt.py --profile"` showing v1's PID 999,
`midicrtd` active). BEFORE capture taken (v1's real content,
`total_mean_brightness=5.31`, `nonblack_px=2.4%`, single color
`(0,255,82)` — matches the known v1 wash signature from phase2/phase3).

**07:27:32** — `tmux send-keys -t midicrt q` (plus, on the first attempt,
an extra literal `Enter` keystroke). **v1 did not quit.** Re-checked at
07:27:34 (`STILL_ALIVE`), 07:28:15 (`STILL_ALIVE`), and after a second
plain-`q` send at 07:29:06 (`STILL_ALIVE`), and after a real `C-c`
(`\x03`) at 07:31:11 (`STILL_ALIVE`) — v1's PID 999 never changed across
~4 minutes of graceful-quit attempts, all well inside what phase2/phase3
needed (`~1.5s`) for the identical keystroke.

**Root-caused, not guessed**: `sudo strace -p 999 -f -e
trace=read,poll,select,ppoll,pselect6 ` for a 3s window showed **zero**
syscalls on fd 0 across any of the process's 12 threads — no thread in
v1 was reading its own stdin/pty at all, despite
`~/codex/midicrt/midicrt.py::keyboard_listener()`'s documented
`with term.cbreak(): while not exit_flag: key = term.inkey(timeout=0.05)`
loop, which should poll fd 0 roughly 20x/second. A direct raw write into
the pty (`printf "q\n" > /dev/pts/0`, bypassing tmux's own `send-keys`
entirely, as an independent test of whether the problem was tmux-side or
app-side) also had zero effect — confirming the read side, not the
delivery mechanism, is where this breaks. `fuser`/`lsof` on `/dev/pts/0`
showed only the expected two processes (`998 zsh`, `999 python`) holding
the fd — no competing reader. This is a real, reproducible finding: **v1's
keyboard-driven quit is currently non-functional on this deployment**,
independent of anything this task changed (v1 is read-only reference,
never modified — confirmed `git`/file state untouched throughout).

## 4. Escalation and its side effect (07:33:16 PDT / 14:33:16 UTC)

Given the graceful path was unresponsive after ~4 minutes with v1's actual
function (rendering, MIDI processing) otherwise completely undisturbed
(zero risk accrued up to this point — nothing had touched `/dev/fb0` or
v1's process yet), one escalation was attempted: `sudo kill -TERM 999`.

**This had a real, undesired side effect**: within the same second,
`journalctl` shows `getty@tty1.service: Deactivated successfully` →
`Scheduled restart job` → `Started`, and the **entire original tmux
server (PID 997) and its pane's shell (998) were gone** (`ps -p 997 998`
returns nothing), replaced by a **brand-new tmux server** (fresh PID,
`session created` timestamp = the exact restart second) running a
**fresh** v1 instance. Root cause, reconstructed from `.zprofile`:

```
if [[ "$TTY" == "/dev/tty1" ]]; then
  ...
  if ! tmux has-session -t midicrt; then tmux new-session -d -s midicrt ...; fi
  exec tmux attach -t midicrt      # tty1's own login shell BECOMES this, via exec
fi
```

The tty1 login shell `exec`s into a `tmux attach` client for the
`midicrt` session — its own process stays the systemd-tracked "main
process" for `getty@tty1.service` (same PID throughout the exec chain).
`tmux new-session -d` daemonizes the SERVER (reparenting it to PID 1 in
the process table), but — per systemd's ordinary cgroup-membership
behavior — a reparented process is NOT automatically moved out of the
cgroup it was forked into, so the tmux server (and everything under it,
including v1) most likely remained inside `getty@tty1.service`'s cgroup.
Killing v1's process apparently caused enough of that cgroup's tree to
unwind that the SESSION itself was torn down (exact mechanism inside the
zsh/tmux teardown not fully isolated — flagged honestly, not papered
over) — the `tmux attach` client on tty1 then saw its session vanish and
exited, which is what `getty@tty1.service` interpreted as
"deactivated," triggering its own (evidently `Restart=`-configured)
respawn: fresh getty → fresh autologin → fresh `.zprofile` → fresh
`tmux new-session` (same name `midicrt`, none of the old process tree
survives) → fresh v1.

**This is a real, disclosed violation of the "tmux session NEVER killed"
hard rule** (`docs/phase2-smoke.md`'s own established contract) — not
because the session was killed directly, but because a signal-based
escalation on the wedged application process had a blast radius wider
than intended, one this task's own author did not fully predict before
acting. Recorded here in full rather than minimized, per this project's
disclosure convention.

## 5. Post-incident verification and the decision to stop

The self-healed v1 instance was verified genuinely alive and rendering:
`pgrep`/`tmux has-session` both healthy, and a fresh read-only `fbcap.py`
capture (`zzz-v1-post-incident-selfheal.png`) showed real content
(`nonblack_px=12.6%`, single color `(0,255,82)` — the same v1 signature,
confirming this is really v1 rendering, not a stale/black frame).
`midicrtd.service` and the pre-existing (unrelated, pre-dating this task)
`/tmp/midicrt_t4_diag` diagnostic daemon were both unaffected throughout.

**Before considering a second pause attempt**, the fresh instance's own
fd-0 read behavior was checked (read-only `strace`, ~8 seconds, ~4900
syscall lines across 14 threads) — **also zero reads/polls on fd 0**.
This is the finding that stopped this task from retrying: the
keyboard-unresponsiveness is not something specific to an
11-hour-old, GIL-starved process instance (which would have been a
reasonable, retry-worthy theory) — it reproduces on a process that was
**under 8 minutes old**. That makes it a systemic property of this
deployment right now, not a fluke of process age, and a second pause
attempt would very likely hit the exact same wall — with the only
"escalation" available (a signal-based kill) already proven to risk
tearing down the whole tmux/getty chain again.

**Decision: Task 7's live-`/dev/fb0` pause window is BLOCKED, not
retried.** Per the task's own governing rule ("BLOCKED on any restore
failure... one retry then BLOCKED"), and given the escalation step
already produced one undesired structural side effect this session does
not fully understand the exact mechanism of, a second live attempt was
judged higher-risk than valuable. This is a judgment call, disclosed for
review rather than asserted as obviously correct.

**Working hypothesis for the underlying regression** (disclosed as a
hypothesis, not confirmed root cause): `progress.md`'s ledger records a
**real reboot** during this same Phase 8 window (the boot-debranding job
— `plymouth.enable=0`, `Theme=details`, `logo.nologo` — "reboot
verified"). A boot-time change to kernel console parameters or the
plymouth/getty console handoff is a plausible way to alter `tty1`'s
console driver behavior enough to break `blessed`'s `cbreak()`/`inkey()`
raw-mode detection for a process whose controlling terminal traces back
to that tty — worth checking first in any follow-up, before assuming a
v1 code regression.

## 6. Cleanup performed (all verified)

- Background `clock_stream.py` process killed (`pgrep` confirms gone).
- `config.toml` restored to its pre-task exact content
  (`capture_auto_start = true`, single line) from a byte-for-byte backup
  taken before the scratch edit; `midicrtd` restarted a second time to
  apply it. Verified pristine: two `midicrt status` polls ~7s apart both
  showed `current_page="eventlog"` (no rotation — confirms
  `pagecycle_interval` is back to its 300s default, not the 15s scratch
  value).
- Session count: 16 (pre-task) → 18 (post-cleanup) — two new production
  capture sessions, both expected/documented `capture_auto_start=true`
  restart artifacts (this task's own two `midicrtd` restarts for the
  pagecycle scratch value), same pattern as every prior Phase 8 task's
  own live-verification restarts.
- `~/t7_smoke_scripts/` (this task's own scratch scripts) and all `/tmp`
  capture files removed from the Pi.
- `git status --short` in `~/midicrt2` clean throughout — this task never
  touched application source, only `config.toml` (restored) and this doc.
- v1 (`~/codex/midicrt`) left running (self-healed fresh instance) and
  independently verified alive; **not** further poked at.

## 7. What a follow-up attempt should do differently

1. Diagnose the fd-0 read regression FIRST (does `keyboard_listener()`'s
   thread even start? does `term.cbreak()` raise and get swallowed
   somewhere? does `blessed.Terminal()`'s stream resolve to a real tty in
   this environment?) as its own, non-time-pressured, read-only
   investigation — ideally by attaching `strace`/a debugger to a FRESH
   v1 launch from the very first second, not discovering it 4 minutes
   into a live pause attempt.
2. If a code/environment fix is found and deployed, re-verify the
   graceful-quit path in isolation (send `q`, confirm gone within the
   phase2/3 precedent's ~1.5-2s) BEFORE structuring a full multi-feature
   demo around it.
3. If the graceful path still cannot be restored, do NOT use a raw
   `kill -TERM`/`-KILL` on the wedged PID again without first understanding
   the getty/cgroup interaction from §4 — consider instead
   `systemctl restart getty@tty1.service` as a single, well-understood
   unit of restart (accepting the same "fresh v1" outcome deliberately,
   rather than as a side effect), or physical/console-level intervention
   with the actual user present.

## Evidence index

`~/projects/pivisualizer/docs/evidence-phase8-smoke/` on motherbase:

| File | What it shows |
|---|---|
| `aaa-v1-before-pause-attempt.png` | v1's real content immediately before the pause attempt (raw fb0 read) |
| `headless-eventlog-marquee-liveBPM.png` | Live production daemon, headless render: marquee mid-scroll, real derived BPM, monochrome |
| `headless-help-overlay-roster-resolved.png` | Live production daemon, headless `--overlay` render: help panel + roster-resolved `page.jump` entries |
| `zzz-v1-post-incident-selfheal.png` | v1's real content from the self-healed fresh instance, confirming it recovered and is genuinely rendering |
