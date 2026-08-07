# Phase 3 Task 11 smoke — supervised all-pages real-CRT run

Task 11 of the Phase 3 (parity ports) plan: the phase finale — every ported
page cycled on the real `/dev/fb0`, in one supervised v1-pause window,
following the discipline this doc's Task-4/phase-2 predecessor established
(`docs/phase2-smoke.md`). Two attempts: the first found and fixed a real
concurrency bug (below); the second is the clean evidence run.

Evidence PNGs live on motherbase, not in this repo (one-off run evidence,
not fixtures): `~/projects/pivisualizer/docs/evidence-phase3-smoke/`.

## Attempt 1 (2026-08-07, ~14:33-14:34 UTC) — crashed, root-caused, fixed

Preconditions verified identically to `phase2-smoke.md` (tmux session
alive, `midicrtd` active, `/dev/fb0` 800×475/16bpp/stride 1600, `billie` in
`video`). Preflight `--out` mode round-trip against the live daemon
confirmed the render path end-to-end before touching v1.

Paused v1 (`tmux send-keys -t midicrt q`, confirmed gone via
`pgrep -f '[m]idicrt.py --profile'` — the bracket trick, since a naive
`pgrep -f "midicrt.py"` self-matches the checking command's own argv over
ssh). Started `midicrt-fb --no-input` on the real device, injected a MIDI
burst, then drove `midicrt action page.goto --arg name=<page>` through the
roster (`eventlog voices harmony pianoroll spectrum img2txtviz config`),
capturing `/dev/fb0` after each via a reused `/tmp/fbcap.py` (phase-2's
throwaway raw-fb-to-PNG script). **Every single page capture came back
essentially pure black** (0.0-0.004 mean brightness, 0-8 non-black pixels
out of 380,000) — including `config`, which has zero MIDI dependency and
should never be blank.

`midicrt-fb`'s own stderr log (`nohup ... > midicrt-fb-run.log 2>&1`)
showed the real cause: it crashed on the very FIRST redraw-loop tick with
`KeyError: 'count'` inside `render_frame` (`fb/app.py:139`,
`f"{vm['title']}  ({vm['count']} events)"`) — `vm['title']` succeeded
("SCREENSAVER"), `vm['count']` didn't. The whole capture loop had been
running against a device `midicrt-fb` was no longer writing to; `/dev/fb0`
never showed anything but its own residual pre-write state.

v1 was still relaunched and verified alive per the hard safety
requirement (this crash is entirely client-side, upstream of any device
write) before root-causing.

### Root cause

`fb/app.py::_run_device`'s and `tui.py::run_tui`'s redraw loops both kept
`vm` from the PREVIOUS page across ticks, only replacing it `if
page_updated` (a fresh snapshot for the CURRENT topic arrived *this*
tick). But `state["page"]`/`state["topic"]` flip immediately on a
`page_changed` event. Per `docs/phase2-notes.md`, a (re)subscribed topic's
own first snapshot can arrive "up to 1/max_rate later" — not
synchronously with the event that triggered the resubscribe. Any of the
five independently-ticking overlay topics (status/alerts/timesig/
beatflash/loopprogress) firing in that gap triggered a repaint anyway,
pairing the NEW page name with the OLD page's stale vm. In this run: the
daemon's `screensaver` behavior had auto-activated from real idle time
before `midicrt-fb` connected, so its startup `page`/`topic` were
`screensaver`/`page.screensaver`; my very first `page.goto eventlog` call
flipped `state["page"]` to `"eventlog"` while `vm` still held
`{"title": "SCREENSAVER"}` (no `count` key) — the next `overlay.status`
tick painted that mismatched pair, crashing `render_frame`.

### Fix

Commit `338c9e4` (`fix: page/vm topic mismatch race in both client redraw
loops`, pushed, CI green run `31189844612`). Both loops now track a
companion `vm_topic` alongside `vm`, updated together only when a
topic-matching snapshot actually arrives; the repaint (body + all chrome,
one `_paint_frame`/`dirty` call) is skipped entirely — not crashed, not
half-painted — until `vm_topic == state["topic"]` again. The overlay
update that triggered the tick isn't lost, it just gets folded into the
next tick that also carries (or already has) a matching vm. Two new
regression tests
(`test_run_device_survives_page_switch_before_new_topics_snapshot_arrives`,
`test_run_tui_survives_page_switch_before_new_topics_snapshot_arrives`)
reproduce the exact race deterministically via a scripted inbox, both
RED-confirmed (`git stash` the fix, rerun, real `KeyError('count')`) before
GREEN. Full suite 677 passed, `ruff check src tests` clean.

## Attempt 2 (2026-08-07, ~14:54-14:55 UTC) — clean, this is the evidence run

Prep (v1 still running, non-destructive): reset the daemon to `eventlog`
and sent a fresh MIDI burst so `last_activity_ts` was current before ever
touching v1 — the daemon's screensaver/pagecycle behaviors only key off
real MIDI activity (`note_on`/`note_off`/`control_change`), not
`page.goto` actions, so this is the only way to guarantee the daemon isn't
mid-idle-transition when the pause window opens.

**BEFORE capture** (`aaa-before-v1.png`) — v1's content immediately
pre-pause: `top12_mean_brightness=28.362`, `nonblack_px=100%`, matching
its known wash signature from phase 2.

**Pause**: `tmux send-keys -t midicrt q` at `14:54:23.187`; confirmed gone
at `14:54:24.722` (bracket-trick `pgrep`); tmux session itself untouched.

**Run**: `midicrt-fb --no-input` started on the real device
(`14:54:27.457`), confirmed running via `pgrep`, MIDI burst injected
(20 notes 60-79, vel 100, 50ms apart), then `midicrt action page.goto`
through the full roster with a 0.6s settle + capture after each:

| Page | nonblack | top12 brightness | notable |
|---|---|---|---|
| eventlog | 13.8% | 105.9 | header bar lit, event lines visible |
| voices | 11.4% | 106.3 | per-channel poly bars |
| harmony | 10.8% | 105.9 | chord/key panel |
| pianoroll | 43.0% | 101.1 | dense note bars, distinct accent color (208,48,48) |
| spectrum | 33.8% | 104.7 | real live-mic bars (USB audio device) |
| img2txtviz | 97.8% | 105.9 | dense procedural ASCII field, MIDI-reactive |
| config | 12.0% | 108.9 | read-only settings/engine-facts viewer |
| screensaver | 0.0% | 0.0 | correctly full black, chrome suppressed (task-9 fix) |

Every page shows genuinely distinct, non-degenerate content — no two
pages share the same brightness/nonblack signature, and `screensaver`'s
true-zero confirms the chrome-suppression fix (task 9) still holds under
the real device path.

**Stop v2** (`14:55:04.187`), confirmed via `pgrep`. **Relaunch v1**
(`tmux send-keys -t midicrt '/home/billie/run_midicrt.sh' Enter` at
`14:55:04.918`); confirmed alive at `14:55:05.413` (new pid, ~0.5s later);
tmux pane showed v1's normal plugin-loading startup log, no errors.

**AFTER capture** (`zzz-after-v1-restored.png`, `14:55:05.593`) — v1 back
on the real CRT: `nonblack_px=14.2%`, header lit `(0,252,80)` matching
v1's exact green signature; visible content is v1's own startup log
(plugin loads, `run_compositor` profile, live `note_on` traffic) plus v1's
*own* stuck-notes plugin independently reporting the same held notes my
MIDI bursts left un-released across all these test runs — genuine,
freshly-computed v1 state, not a stale or borrowed frame.

**Total pause window: `14:54:23.187` → `14:55:05.413` ≈ 42 seconds**,
comfortably under the 3-minute budget. `midicrtd.service` and
`midicrt-web-observer.service` were never touched.

### TUI spot-check (after v1 restored, not time-pressured)

`midicrt tui` in a dedicated tmux session (`midicrt2-tui-check`, separate
from v1's `midicrt` session), driven via the TUI's own `n` (page.next)
key, `tmux capture-pane`d. By this point the daemon's screensaver had
auto-reactivated from real idle time (correctly rendered blank — confirmed
this is intentional design, not a bug, before investigating further);
navigating past it showed `img2txtviz` (dense ASCII field + a genuine
`STUCK CRIT` alert row for the same held notes), `config` (settings/engine
facts, matching the fb capture), `eventlog` (86 real events), and `voices`
(poly bars, channel 1 at 86/86 from the repeated un-released note
injections across every test run in this session). Saved to
`tui-spotcheck-{1,2,3}-*.txt` in the evidence dir. No crash, confirming the
fix holds for the TUI client too.

## Verdict

`midicrt-fb`'s real-`/dev/fb0` path now cycles cleanly through the entire
Phase 3 page roster with distinct, correct content per page, and v1
restores reliably within budget. The attempt-1 crash was a genuine,
disclosed finding — not something papered over — root-caused, fixed with
regression tests in both clients, and re-verified clean on attempt 2.
`midicrt-fb.service` remains installed but **not enabled**; v1 remains the
boot default. See `docs/phase3-parity.md` for the full v1→v2 feature
parity checklist this smoke run supports.
