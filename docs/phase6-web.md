# Phase 6 web guide — `midicrt-web`, deployment posture, live smoke evidence

Written 2026-08-08 at Phase 6 close (Task 3), HEAD `15876a0` (Task 2's fix
round), 1354 tests. Covers the whole web surface built across Phase 6: the
merge + protocol-drift reconciliation (Task 1), the full 14-page observer
parity + capture/alert surfaces (Task 2), and this task's own service unit +
deployment decision + live smoke.

**Revised 2026-08-10 (Phase 9 Task 4)**: two of this doc's own Phase 6
decisions were superseded, not just carried forward. (1) **Control is now
ON by default** (user ruling: "web control ON, no auth") — §2's flag table,
§3, and §4's unit file are rewritten below; §8's live-smoke evidence is
LEFT AS WRITTEN, an accurate historical record of Task 3's own test run
against the flags that existed *then* (`--allow-control`), not a claim
about today's default. (2) **The `midicrtd`-restart silent-freeze bug §7
used to document is fixed** — `bridge.py` now reconnects automatically
(bounded, jittered backoff) instead of freezing; the old troubleshooting
entry is deleted (not just corrected again) and replaced by §10's design
writeup. Full details, decision rationale, test list, and this task's own
live smoke transcript: `task-4-report.md` (this phase's SDD folder).

Companion reading: `docs/phase6-notes.md` (the phase's carry-over design
notes this whole phase implements — item 7 is the deployment decision this
doc documents, now superseded per the note above), `docs/phase3-parity.md`
(the Phase 7 cutover checklist this doc's "revisit at cutover" note points
at — its own §7 carried the freeze bug forward as a cutover checklist item;
also updated, see that doc), `docs/phase5-capture.md` (the capture/replay
subsystem `midicrt-web`'s REC surface is a thin UI over).

---

## 1. What `midicrt-web` is

`src/midicrt/clients/web/app.py` (aiohttp) + `bridge.py` (the
`EngineClient`<->asyncio fan-out) + `page.html` (one static page, thin JS,
no build step, no framework). One `midicrt-web` process holds exactly ONE
`EngineClient` connection to the daemon, shared by every browser tab that
connects — `bridge.py`'s own module docstring covers the fan-out/coalescing
design in full; this doc covers usage and posture, not that internal
plumbing.

It is a **fourth protocol client**, alongside `midicrt` (CLI), `midicrt-fb`
(framebuffer CRT renderer), and the TUI (`midicrt tui`) — all four connect to
the same `midicrtd` engine daemon over its Unix socket
(`/run/midicrt/ctl.sock` by default) and are otherwise independent; running
`midicrt-web` never displaces or restarts any of the others (verified live,
§8).

## 2. Usage

```
midicrt-web [--socket PATH] [--host HOST] [--port PORT] [--read-only]
```

| Flag | Default | Meaning |
|---|---|---|
| `--socket` | `config.toml`'s `socket_path` (`/run/midicrt/ctl.sock`) | Engine daemon socket to connect to. |
| `--host` | `127.0.0.1` | HTTP/WS bind address. The installed unit (§4) overrides this to `0.0.0.0`. |
| `--port` | `8766` (`DEFAULT_PORT` in `app.py`) | v1's own web observer already owns `8765` (`midicrt-web-observer.service`) — v2 picked the next port over specifically so both can run side by side during the parity period; see §4/§6. |
| `--read-only` | off (control is ON) | **Phase 9 Task 4** (user ruling: "web control ON, no auth"): gates `/api/action` server-side AND hides the control UI client-side when passed (both gates exist independently — see §3). Replaces the Phase 6-era opt-IN `--allow-control` flag — inverted, not just re-defaulted: a flag named "allow control" that already defaulted to true would offer no way to turn control back off. |

Routes: `GET /` (the page, with `ALLOW_CONTROL` baked in server-side at
request time), `GET /ws` (the live feed — hello frame, then snapshot/event
frames), `GET /api/describe` (proxies the engine's `describe` request:
roster, actions+arg schemas, keymap), `POST /api/action` (control surface,
gated).

## 3. Security posture: `0.0.0.0:8766`, control ON by default

**Current decision (Phase 9 Task 4, user ruling: "web control ON, no
auth" — supersedes the Phase 6 decision below):** the installed unit
(`packaging/midicrt-web.service`) binds `0.0.0.0:8766` — LAN-reachable, no
auth of any kind — and does **not** pass `--read-only`, so `/api/action`
accepts requests and the served page's control UI (REC toggle, generic
action form, prev/next buttons, per-page nav buttons) renders normally.
Anyone who can reach the port can drive the engine (change pages,
start/stop capture, fire arbitrary actions) — there is still no login, no
token, no per-action permission model (see §6). Pass `--read-only` (an
`ExecStart=` edit, not a runtime toggle — same high-friction-opt-out shape
the old opt-in had, just inverted) to revert a given deployment to
observer-only.

**Why LAN-open rather than loopback:** unchanged reasoning from Phase 6 —
this mirrors v1's own web observer (`midicrt-web-observer.service`, also
`0.0.0.0`, already live on this exact Pi today at `:8765` — see the repo
README's Access section) and the Pi's own general posture: a single-purpose
LAN appliance with no firewall, where VNC (`:5900`) and now this dashboard
are already unauthenticated-but-LAN-scoped. Binding loopback-only here
would not close any real gap while everything else on the box stays open
the same way.

**Why the Phase 6 read-only default flipped:** that decision was explicitly
scoped to the *parity* period (v2's web surface running alongside v1's
`:8765` observer as an additional, optional dashboard nobody depended on
yet) and flagged its own criteria for revisiting: "whether `--allow-
control` becomes the norm once this is a trusted operator's only remote
control path." The user's Phase 9 ruling settles that question directly —
control on, no auth — rather than waiting for a formal Phase 7 cutover
milestone; this is a deliberate, explicit choice, not scope creep.

**Double gate, not just a UI hint (mechanism unchanged, only the default
flipped):** `_handle_action` in `app.py` checks
`request.app[_ALLOW_CONTROL_KEY]` server-side and 403s *before* touching the
bridge whenever `--read-only` IS passed, independent of whatever the served
HTML says — a hand-crafted `curl -X POST /api/action` against a
`--read-only` instance still 403s exactly like a browser would (this
mechanism itself was verified live at Phase 6 Task 3, §8.4 below — that
evidence is unchanged, only which flag reaches it is different now). The
page's own `ALLOW_CONTROL` JS constant only controls whether the control UI
*renders*; it is not itself a security boundary, and isn't relied on as
one.

**Still no auth of any kind** — this was true under the Phase 6 read-only
default and remains true now that control is on: `--read-only` is a binary
control/no-control switch, not a permission system. The Pi's still-open,
still-firewall-less LAN posture (a standing revamp-backlog item in the repo
README) is unchanged by either default and remains its own, separate
project.

## 4. `packaging/midicrt-web.service`

```ini
[Unit]
Description=midicrt2 web dashboard (control on, LAN-open, no auth)
After=midicrtd.service
Wants=midicrtd.service

[Service]
User=billie
ExecStart=/home/billie/midicrt2-venv/bin/midicrt-web --host 0.0.0.0 --port 8766
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

(Phase 9 Task 4: no flag change needed in `ExecStart=` to get control ON —
that's `midicrt-web`'s own default now; pass `--read-only` here instead if
a deployment needs to revert to observer-only. The `Description=` line was
updated to match; every other line is byte-identical to the Phase 6
original.)

No `Group=` override (unlike `midicrtd.service`'s `Group=audio`, needed for
PortAudio, or `midicrt-fb.service`'s `Group=video` +
`SupplementaryGroups=input`, needed for `/dev/fb0`/evdev): `midicrt-web` is
a pure Unix-socket *client* of `midicrtd`, never touches audio/video/input
devices, and needs no supplementary group to connect (matching `User=`
already satisfies the socket's owner-level access).

`After=`/`Wants=midicrtd.service` (not `Requires=`): a soft ordering/pull-in
hint, same convention `midicrt-fb.service` uses — if `midicrtd` isn't up
yet, `midicrt-web` will simply fail to connect and restart on its own
`RestartSec=2` cadence rather than being torn down by systemd if `midicrtd`
ever stops (a `Requires=` would do that; explicitly not wanted here, same
reasoning `midicrt-fb.service` already established).

**Installed 2026-08-08, NOT enabled and NOT started** — `systemctl is-
enabled midicrt-web` reports `disabled`, `is-active` reports `inactive`,
verified live (§8.1). **Redeployed 2026-08-10 (Phase 9 Task 4)** for the
control-on default above (`daemon-reload`'d, still deliberately left
disabled/not-started — standing rule: the user starts it when wanted, this
task does not flip that). Phase 7 (cutover) is expected to enable it per
`docs/phase3-parity.md`'s own sign-off checklist; nothing in Phase 6 or
Phase 9 starts it automatically.

## 5. Parity notes (what "observer parity" means here)

- **Subscription scope, disclosed plainly**: `Bridge.start()` subscribes
  exactly two topics — the current `page.<name>` topic and
  `overlay.status` (`bridge.py`'s own `self.client.subscribe([topic,
  chrome.OVERLAY_STATUS_TOPIC], ...)`). The other overlay topics fb/tui
  paint every tick as dedicated bottom-row chrome — `overlay.alerts`,
  `overlay.timesig`, `overlay.beatflash`, `overlay.loopprogress` — are
  never subscribed here and have no web-rendered row at all; this is a
  deliberate scope cut for this task, not an oversight. The underlying
  *information* mostly still reaches the browser through a different path
  — see the Alert surface bullet below: `analyzers/stucknotes.py` also
  emits a generic `alert` protocol EVENT (independent of the
  `overlay.alerts` snapshot topic), which `WebSink` turns into a
  dismissable banner — but the tui/fb *chrome-row presentation* itself
  (the "alerts win, falls back to timesig" bottom row, the beatflash
  glyph, the loopprogress bar) is not mirrored on the web surface at all.
- **14 renderers**, one per `config.pages`'s default roster entry:
  `eventlog`, `progchanges` (reuses `renderEventlog` verbatim — the two
  pages share an identical `{title, count, lines}` shape by design, see
  `pages/progchanges.py`'s own docstring), `voices`, `harmony`, `pianoroll`
  (`<canvas>`, normalized `[0,1]` coordinates scaled to pixels, same 8-hue
  channel palette as `clients/fb/app.py`'s `_ROLL_CHANNEL_PALETTE`),
  `spectrum` (`<canvas>` bars + peak-hold ticks, `available: false` shows
  "no audio input" instead of an empty canvas), `ccmonitor`/`ccdashboard`
  (tables; ccdashboard adds a value bar + LIVE/age label), `chordkey`,
  `sendnotes`, `help`/`config` (share one `labelValueRows()` helper — both
  pages already emit flat `{label, value}` rows), `img2txtviz` (`<pre>`
  grid at native resolution, not upsampled), `screensaver` (blank, matching
  every other client). `PAGE_RENDERERS`'s keys are quoted strings
  specifically so the served JS is greppable for `"<name>":` per page
  without a browser — both `tests/test_web_app.py`'s
  `test_index_page_renderer_registry_covers_full_default_roster` and this
  task's own live curl smoke (§8.2) rely on that.
- **Raw-JSON fallback**: any topic with no dedicated renderer (a future page
  this build predates) still renders — `renderUnknown()` shows the topic
  name + the raw snapshot dict — rather than going blank or throwing.
  Additive-only wire compatibility, same stance `handleMessage`'s event
  dispatch takes for unrecognized event names.
- **Alert surface**: `alert` events render as dismissable, stacked banners
  (`level === "crit"` gets a redder style; anything else — including a
  missing `level` — gets the default amber). Alerts get their own bounded
  (`_ALERT_QUEUE_CAP = 16`), sequence-keyed delivery slots in `WebSink`
  (never coalesced with each other or with the EOF sentinel) specifically
  because `analyzers/stucknotes.py` documents a real, undebounced burst
  mechanism (rapid CC64 sustain-toggling on an already-stuck note); past the
  cap, the oldest pending alert is evicted and a cumulative `dropped_alerts`
  count is stamped onto every alert delivered from that point forward
  (never reset once attached) — `formatAlert()` appends `(+N earlier
  alert(s) dropped)` to the banner when present, so the backend's honesty
  about drops is never silently thrown away client-side.
- **REC surface**: `overlay.status`'s `rec` boolean drives a `.recording`
  CSS class on the status bar, and the status *string* itself carries
  `chrome.REC_MARKER` (`"● REC  "`) — rendered server-side via the exact
  same `chrome.status_text()` function every other client's status row
  calls (Task 1's merge-reconciliation fix: one rendering path, not a
  second JS copy that can drift). Behind the control surface (ON by
  default since Phase 9 Task 4; hidden if started with `--read-only`): a
  dedicated `#rec-toggle` button (posts `capture.start`/`.stop`) plus a `#last-
  session` panel, both kept current by `refreshCaptureStatus()` — which
  POSTs the existing `capture.status` action (no new endpoint) rather than
  trusting the `capture_started`/`capture_stopped` event payloads directly
  (the normal-stop event lacks `started_ts`/`ended_ts`; `capture.status`'s
  `last_session` is the one authoritative source).
- **Learn surface**: `learn_armed`/`learn_bound`/`learn_cancelled` render as
  transient toasts (auto-removed after 4s), reading each event's actual
  payload shape (`learn_bound`'s bound action name lives at
  `data.binding.action`).
- **`dropped_alerts` counter**: see the alert bullet above — this is the
  one piece of state that survives an eviction rather than resetting, by
  design (documented in `bridge.py`'s `WebSink._offer_alert` docstring).

## 6. Known limits

- **Dict-typed args in the generic action form**: `/api/describe`'s
  `actions` map carries each action's arg schema (e.g. `bind.learn`'s
  `args: {"action": "str", "mode": "str", "args": "dict", "range": "str"}`)
  and `renderActionArgs()` builds one plain `<input type=text>` per arg
  regardless of declared type — every value is collected as a raw string
  (`args[inp.dataset.arg] = inp.value`), so an arg typed `"dict"` has no way
  to be entered correctly through this form (typing JSON text into the box
  submits it as a *string*, not a parsed object). Actions with only
  `str`/plain-scalar args (the large majority) work fine through the form;
  `bind.learn` specifically does not, and has no web-side workaround today.
- **Replay is CLI-only**: `midicrt replay <file> [--speed N | --instant]`
  has no web equivalent — `docs/phase5-capture.md` §6 covers replay's own
  disclosed limitations (tick-driven state freezes during replay, etc.),
  all of which apply identically regardless of client; the web surface
  simply doesn't expose replay at all, by scope, not by omission.
  `capture.start`/`.stop`/`.status`/`.pin` (live recording, not replay) are
  the only capture-adjacent actions reachable from the web.
  **`capture_dir` note (this task's own finding, generalizing T2's):**
  wherever `midicrt-web` runs, the actual recording still happens
  **inside the daemon it's connected to** — `midicrt-web` has no
  `--capture-dir` flag of its own and cannot isolate a capture by itself;
  a capture always lands in whatever `Config.capture_dir` (or the
  StateDirectory/dev-fallback default) the *daemon* was started with (see
  `engine/capture.py::resolve_capture_dir`). Testing REC through the web
  UI against the production daemon means testing against the production
  `/var/lib/midicrt/sessions` — this task's own live smoke avoided that
  entirely by pointing at a scratch daemon started with an explicit
  `capture_dir` override instead (§8.3), rather than writing-then-cleaning-
  up in the shared production path as Task 2's manual verification did.
- **Subscribe rate: 5/s**, not the 10/s the pre-merge branch shipped with
  (`bridge.py`'s `DEFAULT_SUBSCRIBE_RATE`) — per phase6-notes item 6's spec
  value. A browser tab is a slower consumer than a terminal render loop, and
  `WebSink`'s own drop-and-replace queue can only ever hold one pending
  frame per topic regardless of rate, so a higher push rate here would be
  pure waste, not smoother rendering.
- **No auth of any kind**, at any control level — see §3. `--read-only`
  is a binary control/no-control switch, not a permission system; there is
  no notion of a read-only-but-authenticated user, or of per-action
  permissions.
- **One `EngineClient` connection per `midicrt-web` process**, shared by
  every browser tab via `Bridge`'s fan-out — this is by design (`bridge.py`
  §"Multi-browser fan-out"), not a limit needing a fix, but worth knowing:
  a slow tab never blocks others, but every tab's `page.<current>` topic is
  driven by whichever page THE ENGINE is currently on process-wide, not a
  per-tab virtual page (all tabs see the same page; there is no per-browser
  page navigation independent of the shared current-page state visible to
  every other client of the daemon, web or otherwise).

## 7. Troubleshooting

- **`/api/action` 403s even though I expected control to be on**: control
  is ON by default (Phase 9 Task 4) — a 403 here means the unit's
  `ExecStart=` has `--read-only` in it. `systemctl cat midicrt-web` to
  check what's actually configured; `sudo systemctl daemon-reload` after
  any edit. `--read-only` is not a runtime toggle either way.
- **Page shows "no renderer for `page.X` yet — raw snapshot below"**: either
  a genuinely new page this build predates (fine — it's still usable, just
  unstyled), or a typo'd/renamed page name in `config.toml`'s `pages` list
  that no longer matches a `PAGE_RENDERERS` key.
- **A `midicrtd` restart used to leave `midicrt-web` silently frozen —
  fixed, Phase 9 Task 4.** This was previously the single longest entry in
  this section (a `Bridge._pump_loop()` design gap: an engine-side EOF was
  read once and never reconnected, so a browser's own honest websocket
  reconnect landed on a bridge that replayed a normal-looking but
  permanently stale `hello`). It is deleted here, not just corrected again
  — the underlying bug is fixed, not merely better-documented. See §10 for
  the current reconnect design and `task-4-report.md` for the live smoke
  transcript proving it. If a dashboard ever again looks "connected" but
  stops updating, that is now a genuine bug report, not this known issue.
- **Port already in use**: v1's observer owns `:8765`; v2's is `:8766` by
  default (`DEFAULT_PORT`) specifically to avoid the collision — verified
  live, no conflict, both ports independently reachable (§8.1/§8.6).

## 8. Live smoke evidence (on-Pi, real `midicrtd` build; scratch engine +
scratch `midicrt-web` instances; motherbase as the LAN client throughout)

Full transcripts, exact commands, and pristine-state proofs are in
`task-3-report.md` (this phase's SDD folder) — this section summarizes what
was verified and how, per the brief's explicit ask to isolate captures
rather than write into the shared production `/var/lib/midicrt/sessions`
(a gotcha Task 2's own manual verification flagged for this task).

**Isolation strategy**: rather than pointing at the live production
`midicrtd` (pid unchanged throughout, never restarted, never sent anything
but this section's own read/observe traffic), this task's smoke ran a
*separate* scratch `midicrtd --no-midi --config <scratch config.toml with
capture_dir override>` under a `systemd-run --user` transient unit, plus
two transient `midicrt-web` instances against it — one with the exact
production flags (`--host 0.0.0.0 --port 8766`, no `--allow-control`), one
with `--allow-control` on a second port as a control-mode sanity companion.
All three transient units were stopped and reset-failed afterward; the
scratch socket/config/capture directory were removed; production's
`/var/lib/midicrt/sessions` was confirmed empty both before and after
(never touched at all, not just cleaned up afterward).

1. **Unit installed, not enabled**: `/etc/systemd/system/midicrt-web.service`
   installed via `sudo install` + `daemon-reload`; `systemctl is-enabled` →
   `disabled`, `is-active` → `inactive`; content byte-identical to the repo
   copy. Port `8766` confirmed free before, and free again after the
   smoke's transient instances were stopped.
2. **HTML renderer-registry markers**: `curl http://pivisualizer.internal:
   8766/` from motherbase → HTTP 200, 31941 bytes; all 14 `PAGE_RENDERERS`
   keys (§5's list) found via `"<name>":` grep against the served body —
   same assertion `test_index_page_renderer_registry_covers_full_default_
   roster` makes server-side, done here against a real network response.
   The read-only instance's served page carries `ALLOW_CONTROL = false`;
   the control-sanity instance's carries `ALLOW_CONTROL = true` — confirmed
   both, from the same two curls.
3. **WS `hello`/`describe` + `page.goto` across 5 pages**: a small aiohttp-
   based Python WS client (motherbase, ephemeral venv, removed after)
   connected to the read-only instance's `/ws`, received a `hello` frame,
   then `midicrt action page.goto --arg name=<page>` was run via the CLI
   against the scratch daemon's socket (**not** through the web control
   endpoint — the read-only instance never handled these navigations) for
   `harmony`, `pianoroll`, `spectrum`, `ccdashboard`, `chordkey` in
   sequence. Every one produced a real `page_changed` event followed by a
   non-fallback snapshot with the correct page-specific `title`
   (`"HARMONY"`, `"PIANOROLL"`, `"SPECTRUM"`, `"CC DASHBOARD"`,
   `"CHORD+KEY"`) over the websocket — proving the read-only dashboard
   correctly observes engine-driven navigation it cannot itself trigger.
4. **REC flow + 403 verification**: `capture.start` (CLI, scratch socket) →
   the read-only instance's websocket showed a `capture_started` event
   followed by an `overlay.status` snapshot whose `status_text` now leads
   with `chrome.REC_MARKER` (`"● REC  ..."`); `capture.stop` (CLI) →
   `capture_stopped` event + status_text reverting to no marker;
   `capture.status` (CLI) afterward showed `last_session` with the matching
   session id/timestamps. The session file landed in the scratch
   `capture_dir` (`index.json` + one `session-*.jsonl`), confirmed via
   `ls` — production's `/var/lib/midicrt/sessions` stayed empty throughout
   (checked before capture.start and after cleanup). Separately: `curl -X
   POST /api/action` with a `page.goto` body against the READ-ONLY instance
   → HTTP 403 `{"error": "control disabled (start midicrt-web with
   --allow-control)"}`; the identical request against the ALLOW-CONTROL
   sanity instance → HTTP 200 `{"page": "eventlog"}` — proving the 403 is
   the `--allow-control` gate specifically, not a broken endpoint.
5. **Alert surface — verified via engine test-fixture, not live hardware
   (disclosed per this task's own brief)**: the scratch engine ran
   `--no-midi` (no real MIDI input at all, by design, to keep the smoke
   fully isolated from the Pi's real, currently-connected synth rack and
   from the live production daemon's own MIDI routing); reliably triggering
   `analyzers/stucknotes.py`'s alert path needs a real held note crossing
   `WARN_AFTER` (2s), which was judged not worth the risk of injecting
   synthetic MIDI into either the live production daemon (which shares the
   Pi's real ALSA sequencer graph with actual connected instruments) or a
   second real-MIDI scratch instance (which would necessarily overlap that
   same shared ALSA graph, defeating the isolation). Instead, ran
   `tests/test_web_bridge.py -k alert` on the Pi against this exact HEAD:
   `test_bridge_fans_out_alert_event` (a REAL near-zero-`WARN_AFTER`
   `StuckNotesAnalyzer` alert, injected via `eng.queue.put()`, driven
   through the real `ProtocolServer`/`EngineClient`/`Bridge` wire — not a
   synthetic dict or `eng.add_listener()`) plus the three burst/cap/EOF-
   ordering tests from Task 2's review fix — all 4 passed. This proves the
   ws fan-out path for `alert` end-to-end at the protocol level; it does
   not additionally prove it against this task's own live scratch instance
   specifically, which is the disclosed gap.
6. **v1 untouched, no port conflict**: `midicrt-web-observer.service`
   (`:8765`, pid unchanged) confirmed still listening throughout; production
   `midicrtd` (pid unchanged, `ActiveState=active`) confirmed untouched
   before, during (aside from the CLI actions in §8.3/§8.4, which targeted
   the scratch daemon, never production), and after.

## 9. Test suite

`tests/test_web_bridge.py` (20 tests) + `tests/test_web_app.py` (13 tests) —
33 tests total, part of the full suite's 1354/1354 passing at this doc's
HEAD (`15876a0`). `ruff check src tests`: clean.

**Updated 2026-08-10 (Phase 9 Task 4, including its own review-fix
round):** `tests/test_web_bridge.py` (33 tests, +13: the reconnect loop,
backoff shape, `bridge_status`/`hello` status field, the
web-action-arms-pagecycle-pause integration test, and — review fix — the
malformed-peer-line and unexpected-exception reconnect-survival tests) +
`tests/test_web_app.py` (17 tests, +4: the `bridge_status` JS content pin
and the `--read-only` CLI flag semantics) + `tests/test_client_base.py`
(39 tests, +2: review fix — `EngineClient`'s own read paths treating a
malformed line as connection-lost, not an uncaught `JSONDecodeError`) —
**89 tests across the three files, 19 new/changed overall**, part of the
full suite's **1783/1783** passing at this task's own final HEAD (grown
from the 1779 first landed, per the review's own fix round). `ruff check
src tests`: clean. Full test list and design rationale: `task-4-report.md`
(including its "Review response" section).

## 10. Engine-restart reconnect (Phase 9 Task 4)

**What used to happen:** see the deleted §7 entry's replacement note above
— a `midicrtd` restart silently froze `midicrt-web` forever, surviving only
a manual `midicrt-web` restart.

**What happens now (user ruling: "option B"):** an already-open browser
websocket is untouched by an engine restart — no close frame, no
reconnect on the browser's own leg. Only the bridge's OWN `EngineClient`
connection to `midicrtd` needs to recover, and now does: `Bridge._pump_
loop` treats an engine-side EOF as "reconnect", not "give up" — it clears
`engine_hello`/`_latest` immediately (so a websocket that connects mid-
outage gets an honest empty hello, not a stale-but-normal-looking one),
fans a `bridge_status: reconnecting` event out to every already-open sink,
then retries `connect()` against a **brand-new** `EngineClient` (the dead
one can never restart its own reader thread) with bounded, jittered,
doubling backoff — 500ms first retry, doubling, capped at 10s, ±20%
jitter (`bridge.py`'s `DEFAULT_RECONNECT_BASE_DELAY`/`_MAX_DELAY`/
`_JITTER`), deliberately the SAME shape `page.html`'s own browser-side
reconnect already used, so the two reconnect loops this feature now has
read as one consistent posture. Retries run **indefinitely** — the daemon
may be down for minutes (a real `systemctl restart midicrtd`, not a
sub-second blip) — until it succeeds or `midicrt-web` itself shuts down.

Once reconnected, the bridge re-derives the CURRENT page **fresh** from the
newly-connected engine (not the pre-outage topic — `midicrtd` restarting is
a new process with no guarantee its default current page matches what
browsers were looking at before) and fans a `bridge_status: connected`
event plus, if the page actually changed, a synthesized `page_changed`
event so `page.html`'s nav highlight stays honest. The next real snapshot
for that topic then reaches already-open sinks completely normally — every
`PAGE_RENDERERS` entry replaces `#page-body`'s content wholesale off that
snapshot's own data, never merges into stale DOM, so "drop-and-replace" on
reconnect needed no client-side fix, only verifying it (done — see
`page.html`'s renderers).

The ONE case Option B does NOT change: a genuine `midicrt-web` process
shutdown (`bridge.stop()`, e.g. `systemctl stop midicrt-web`) still closes
every open sink (the pre-existing EOF-sentinel-to-sinks behavior,
preserved for this one case via `Bridge._on_shutdown`) — there is no
"fresh snapshot" to eventually resume with once the process itself is
gone.

Full design rationale (backoff shape, status surfacing, flag semantics),
test list, and this task's own live smoke transcript (a scratch `midicrtd`
restarted mid-websocket-session, showing gap → reconnect → fresh snapshot
flow resuming on the SAME websocket): `task-4-report.md` (this phase's SDD
folder). `bridge.py`'s own module docstring ("Engine-restart reconnect"
section) is the from-the-source-code version of this same design.
