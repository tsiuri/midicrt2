# midicrt2

MIDI-CRT visualizer, v2: `midicrtd` engine daemon + fb/tui/web protocol clients.
Design spec: `pivisualizer` project folder on motherbase
(`~/projects/pivisualizer/docs/superpowers/specs/2026-08-06-midicrt2-design.md`).

Dev happens on-device (the Pi). Venv: `~/midicrt2-venv`.
Run tests: `~/midicrt2-venv/bin/pytest`

## Docs

- `docs/phase6-web.md` — `midicrt-web` usage/flags, the `0.0.0.0:8766`
  LAN-open security posture + rationale (control ON by default since Phase
  9 Task 4's `--read-only` opt-out flag, user ruling: "web control ON, no
  auth" -- supersedes the earlier Phase 6 parity-era read-only default),
  14-page renderer parity notes, known limits (dict-args in the generic
  action form, replay stays CLI-only, 5/s subscribe rate), the bridge's
  engine-restart reconnect design (bounded/jittered backoff, browsers keep
  their websockets across a `midicrtd` restart), and live smoke evidence.
  Read this before installing/enabling `packaging/midicrt-web.service` or
  building anything against the web protocol surface.
- `docs/phase5-capture.md` — event-sourced session capture format spec
  (header/event/action-mark/page_changed/tempo lines, provenance origins),
  `index.json`/retention/pin, loss-window + write-failure containment,
  `midicrt replay` usage + suppression semantics + the tick-freeze and
  transport-running limitations, learned-binding port durability
  (`bind.list`'s `port_present`), `--range` for continuous learn, and the
  help page's live keymap section. Read this before building anything
  against captured session files or the replay pipeline.
- `docs/phase4-bindings.md` — keymap.toml/bindings.toml schema reference,
  `midicrt bind learn/list/remove/cancel` usage, trigger-vs-continuous
  guidance, roster-dependence caveats, troubleshooting. Read this before
  hand-editing either file or building a client against the bindings API.
- `docs/phase3-parity.md` — v1→v2 feature parity checklist (Phase 7
  cutover sign-off document).
- `docs/phase1-smoke.md`, `docs/phase2-smoke.md`, `docs/phase3-smoke.md` —
  supervised real-CRT/real-hardware smoke evidence for each phase.
- `docs/visual-audit.md` — the Phase 8 v1-vs-v2 visual/animation feature
  parity checklist (PRESENT/DIFFERENT/MISSING/N-A per row, build-priority
  ranking). The load-bearing document for the whole GUI-parity phase.
- `docs/phase8-smoke.md` — Phase 8 Task 7's supervised real-CRT smoke:
  **DONE** on the second attempt (v1 paused via `send-keys q` in ~2s,
  full GUI-phase demo captured on the real `/dev/fb0`, v1 restored and
  verified, total downtime ~2m46s). **Read this before pausing v1**: the
  first attempt misdiagnosed v1's keyboard-quit as broken because its
  `strace` filter didn't cover `pselect6` (the actual syscall `blessed`'s
  readiness check uses on this platform) and its grep didn't match that
  syscall's `[0]`-style fd notation — v1's quit was never actually
  broken. That misdiagnosis led to a `kill -TERM` escalation that
  cascaded into a real tmux/getty teardown (self-healed) — **never signal
  v1's process tree**; if `send-keys` ever seems ineffective, verify with
  `strace -e trace=pselect6` (watch for `pselect6(1, [0], ...)`) before
  concluding anything, per the binding escalation-rule amendment in that
  doc.
