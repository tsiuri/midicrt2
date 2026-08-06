# Phase 2 smoke test — supervised real-CRT run (on the Pi)

Task 4 of the Phase 2 (CRT client) plan: the only window in which v1
(`~/codex/midicrt/midicrt.py`, tmux session `midicrt`) may be paused, and it
must come back. This exercises `midicrt-fb`'s real-`/dev/fb0` path
(`_run_device`/`Surface.write_fb`/`_read_fb_geometry`) for the first time —
Tasks 1-3 only ever exercised `--out` PNG mode against a live-but-headless
daemon.

Run performed 2026-08-06, ~16:41-16:42 UTC. Evidence PNGs live in the SDD
workspace on motherbase (`~/projects/pivisualizer/.superpowers/sdd/2026-08-06-midicrt2-phase2-crt-client/{before,during,after}.png`)
— not checked into this repo.

## Preconditions verified before touching v1

- `tmux has-session -t midicrt` — OK, session `midicrt` alive (1 window,
  attached).
- `midicrtd.service` (system unit) — active, listening on
  `/run/midicrt/ctl.sock` (owner `billie:audio`, `srwxr-xr-x` — no sudo
  needed to connect).
- `/dev/fb0` — `crw-rw---- root:video`, 800×475, `bits_per_pixel=16`,
  `stride=1600` (== width×2, tightly packed, matches Surface's default).
  `billie` is in the `video` group — direct read/write, no sudo.
- Pre-flight (non-destructive, v1 still running throughout): a throwaway
  `/tmp/fbcap.py` script on the Pi (read raw `/dev/fb0`, unpack RGB565 LE via
  `array("H")`, build a PIL RGB image, save PNG + print brightness/probe-pixel
  stats) was written and test-run against the live fb (reads only — never
  writes). `midicrt-fb --out /tmp/preflight.png` was run twice against the
  live daemon (once before, once after injecting MIDI) to confirm the
  client/daemon/render path end-to-end before ever touching the real device.

## The smoke window

1. **BEFORE capture** (`before.png`) — v1's content immediately pre-pause:
   a multi-column tabbed event log (`STOP`, `Piano Roll`, `Audio Spectrum`,
   `Tuner`, `Chord+Key`, `Stuck Heatmap`, `Voice…` tabs across the top),
   dense per-note `note_on`/`note_off` columns, transport/status line at the
   bottom (`BAR`, `TIMER`, `fps`). Stats: `total_mean_brightness=16.77`,
   `top12_mean_brightness=28.23`, `nonblack_px=380000/380000 (100%)` — v1
   paints a faint uniform wash (`(0,8,0)`) even over "empty" background, so
   the whole frame reads as non-black.

2. **Pause v1**: `tmux send-keys -t midicrt q`. ~1.5s later,
   `pgrep -af 'midicrt.py'` returned no process match (only the grep
   invocation itself) — v1's Python process was gone. `tmux has-session -t
   midicrt` still reported the session alive (v1 quit to the shell inside
   the pane, exactly per the safety contract — the tmux session itself was
   never touched).

3. **Run v2 on the real fb**: `nohup timeout 30 ~/midicrt2-venv/bin/midicrt-fb
   --no-input > /tmp/midicrt-fb-run.log 2>&1 &` (backgrounded so the
   controlling shell could continue driving MIDI injection and captures
   concurrently). Confirmed both `timeout` and the `midicrt-fb` child were
   running via `pgrep`.

4. **Inject MIDI** for a lively frame: the phase-1 one-liner
   (`docs/phase1-smoke.md` step 3) sending 20 `note_on` messages
   (notes 60-79, velocity 100, 50ms apart) via `mido` on the `Midi Through:Midi
   Through Port-0` port matched by the daemon's default `["*"]`
   `midi_sources` pattern.

5. **DURING capture** (`during.png`) — v2's real-device render, unmistakably
   different from v1's layout: a single reverse-video header bar reading
   `EVENT LOG  (56 events)` spanning the full width, then a single column of
   accent-colored `note_on chN nNN vNNN` lines (the just-injected notes,
   60-79 twice over — the earlier pre-pause injection plus this one both
   landed in the daemon's eventlog and both digests appear). Stats:
   `total_mean_brightness=7.98` (dimmer overall — v2's background is
   genuinely `(0,0,0)` black, no wash), `top12_mean_brightness=105.95`
   (**~3.75× BEFORE's 28.23** — the full-width bright-green header fill is
   the dominant unambiguous v2 signature), `nonblack_px=27393/380000
   (7.2%)` (vs. BEFORE's 100% — confirms a real black background, not v1's
   wash). Probe pixels: `header_left(4,4)=(0,252,80)`,
   `header_mid(400,4)=(0,252,80)` (both inside the header bar — bright
   across its full width, unlike v1 where only the left tab-label region is
   lit), `body_left(4,50)=(0,252,80)` (an accent event line),
   `body_mid(4,200)=(0,0,0)` (true black — the single event column is far
   narrower than v1's multi-column layout, so most of the body is empty).

6. **Stop v2**: `pkill -f 'midicrt-fb --no-input'`; confirmed gone via
   `pgrep`.

7. **Relaunch v1**: `tmux send-keys -t midicrt '/home/billie/run_midicrt.sh'
   Enter`. Within ~3s, `pgrep -af 'midicrt.py'` showed the new PID running
   `python midicrt/midicrt.py --profile run_compositor` (matches
   `run_midicrt.sh`'s hardcoded `--profile run_compositor`, same as the
   original launch); `tmux capture-pane` showed the normal plugin-loading
   startup log with no errors.

8. **AFTER capture** (`after.png`), taken ~8s after relaunch — v1 back on
   the real CRT, mid-startup on a different page than BEFORE (v1 cycles
   pages via `plugins.pagecycle`): a channel/patch list, chord/scale
   detection panel, and a "welcome to the jungle" waveform widget, with a
   live transport line (`BAR 0000 BEAT 01 TICK 000`, wall-clock timestamp
   `09:42:23.190` — consistent with the capture's UTC timestamp, confirming
   this is a live, ticking process and not a stale frame). Stats:
   `nonblack_px=380000/380000 (100%)` (matches BEFORE's signature wash —
   this alone rules out it still being v2's frame) and
   `top12_mean_brightness=27.98` (within 1% of BEFORE's 28.23, v1's
   characteristic header brightness). `total_mean_brightness=6.83` is lower
   than BEFORE's 16.77 simply because this page has much more black
   background than the dense multi-column event log page BEFORE was on —
   not a sign of anything broken.

**Total pause window: v1 process gone (`tmux send-keys q`) to v1 confirmed
alive + AFTER capture saved: ~46 seconds** (16:41:39Z → 16:42:25Z),
comfortably under the ~3 minute budget. `midicrtd.service` and
`midicrt-web-observer.service` were never touched and stayed up throughout.

## Verdict

`midicrt-fb`'s real-`/dev/fb0` path (geometry read from sysfs, RGB565 packing,
device write, evdev-optional input loop) works end-to-end against the live
daemon on the actual hardware. v1 was paused for well under the budget and
came back verified-alive with its content restored, satisfying the hard
safety requirement. Evidence PNGs (`before.png`/`during.png`/`after.png`) and
full capture stats are preserved outside this repo in the SDD workspace on
motherbase (not committed here — they're one-off run evidence, not
fixtures).

`midicrt-fb.service` remains installed but **not enabled**; v1 remains the
boot default. Enabling `midicrt-fb.service` (cutting over the CRT to v2 by
default) is a later-phase decision, not part of this task.
