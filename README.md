# midicrt2

MIDI-CRT visualizer, v2: `midicrtd` engine daemon + fb/tui/web protocol clients.
Design spec: `pivisualizer` project folder on motherbase
(`~/projects/pivisualizer/docs/superpowers/specs/2026-08-06-midicrt2-design.md`).

Dev happens on-device (the Pi). Venv: `~/midicrt2-venv`.
Run tests: `~/midicrt2-venv/bin/pytest`

## Docs

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
