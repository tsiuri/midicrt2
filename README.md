# midicrt2

MIDI-CRT visualizer, v2: `midicrtd` engine daemon + fb/tui/web protocol clients.
Design spec: `pivisualizer` project folder on motherbase
(`~/projects/pivisualizer/docs/superpowers/specs/2026-08-06-midicrt2-design.md`).

Dev happens on-device (the Pi). Venv: `~/midicrt2-venv`.
Run tests: `~/midicrt2-venv/bin/pytest`

## Docs

- `docs/phase6-web.md` — `midicrt-web` usage/flags, the `0.0.0.0:8766`
  read-only-by-default security posture + rationale (v1-observer parity,
  revisit at Phase 7 cutover), 14-page renderer parity notes, known limits
  (dict-args in the generic action form, replay stays CLI-only, 5/s
  subscribe rate), and live smoke evidence. Read this before installing/
  enabling `packaging/midicrt-web.service` or building anything against the
  web protocol surface.
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
