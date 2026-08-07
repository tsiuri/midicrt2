# Phase 4 bindings guide — keymap.toml + bindings.toml + MIDI learn

Written 2026-08-07 at Phase 4 close (Task 4), HEAD `bfdef13` (1148 tests
green, ruff clean). Covers the two binding layers built across Tasks 1–3:
a config-served **keymap** (one keypress → one action, resolved client-side)
and an engine-side **MIDI bindings** dispatcher (a raw MIDI event → an
action, with zero clients attached), plus **DAW-style learn** (arm the
engine, touch a control, get a persisted binding back — no file editing).

Both layers live under `~/.config/midicrt/`, alongside the pre-existing
`config.toml`. All three files are engine-**readable**; only `bindings.toml`
is ever engine-**written** (exclusively by `bind.learn` captures and
`bind.remove` — see "Machine-managed header + write discipline" below).

| File | Purpose | Who writes it |
|---|---|---|
| `config.toml` | Pages roster, instruments, tick rate, etc. | Human only |
| `keymap.toml` | One-character key → action | Human only |
| `bindings.toml` | MIDI event → action (trigger/continuous) | Human **or** `midicrt bind learn`/`bind remove` |

---

## 1. `keymap.toml` schema reference

```toml
[keys]
"q" = "client.quit"
"c" = "eventlog.clear"
"n" = "page.next"
"p" = "page.prev"        # example: not bound by default (see below)
```

One table, `[keys]`, mapping a single-character string key to an action
name. Read by `engine/keymap.py::load_keymap`; served to every client over
`describe`'s `"keymap"` field; re-read live via the `config.reload` action.

| Field | Type | Notes |
|---|---|---|
| `[keys].<key>` | string → string | `<key>` is the literal character a client's keypress resolves to (case-sensitive, one character by convention — nothing enforces length, but both clients only ever emit single characters). Value is an action name, or a `client.*` pseudo-action. |

**Defaults** (`DEFAULT_KEYMAP`, always present as the floor): `q` →
`client.quit`, `c` → `eventlog.clear`, `n` → `page.next`. No `p` binding
ships by default — `page.prev` has always existed as an action but was
never wired to a key in either client before Phase 4; bind it yourself via
`keymap.toml` if you want it.

**Merge, not replace.** A present `keymap.toml`'s `[keys]` table is merged
**on top of** `DEFAULT_KEYMAP`, key by key — it does not need to (and should
not) redeclare `q`/`c`/`n` just to add one remap. This is a deliberate
departure from `config.toml`'s own whole-field-replacement convention,
because whole-table replacement is a real footgun for keybindings
specifically: a user remapping one key would otherwise silently lose `q`
(quit) unless they thought to redeclare it. Mirrors how editor
`keybindings.json`-style override files work.

**Pseudo-actions.** Any action name starting with `client.` never reaches
the engine's action registry at all — it is resolved entirely client-side
by `clients/base.py::dispatch_key`. Today only `client.quit` has real
meaning; any other `client.*` value (e.g. a typo) is a harmless local
no-op, never sent to the engine.

**Validation timing: never at load, always at the registry boundary.**
`load_keymap` is pure TOML parsing + merge with zero registry awareness —
the action vocabulary is roster-dependent (`pianoroll.*`, `sendnotes.key`,
etc. only exist when their owning page is in `config.pages`), so there is
nothing to validate against yet. `filter_known_actions` is the separate
step, run by `Engine.__init__` and again by `config.reload`, that drops
(logs a warning, never raises) any entry whose action:
- is neither a `client.*` pseudo-action nor a currently-registered action, or
- **requires arguments.** A single keypress can never supply argument
  values, so any action with a non-empty `args` schema is rejected at this
  step even if the name is real. Six shipping actions are affected today:
  `sendnotes.key`, `page.goto`, `pianoroll.zoom`/`.projection`/`.channels`,
  `pagecycle.enable`.

**Malformed-file resilience.** A syntactically invalid `keymap.toml`, or one
with the wrong *shape* at `keys` (e.g. `keys = "oops"`, `keys = 5`, `keys =
["a"]` — all legal TOML, none a table), never crashes the daemon:
`load_keymap_or_warn` catches it and the caller falls back to
`DEFAULT_KEYMAP` (at boot) or the last-good keymap (at `config.reload`),
appending a human-readable warning either to the daemon log (boot) or the
`config.reload` response's `warnings` list (reload) — the requesting
connection is never torn down. An individual bad entry inside an otherwise
valid table (e.g. `n = 5`, a non-string value) is skipped with its own
logged warning; the rest of the table still loads.

---

## 2. `bindings.toml` schema reference

```toml
# midicrt bindings.toml -- machine-managed by `midicrt bind` operations
# (learn/remove). Hand-editing is supported (loaded like any other file),
# but comments and formatting here are NOT preserved: any save rewrites
# this whole file from the engine's in-memory binding list. See
# docs/phase4-bindings.md for the schema reference.

[bindings.learn_1786140583127]
action = "page.next"
mode = "trigger"
threshold = 64

[bindings.learn_1786140583127.match]
type = "note_on"
number = 60
channel = 0
port_pattern = "Midi Through:Midi Through Port-0 14:0"

[bindings.cc_swell]
action = "pianoroll.zoom_level"
mode = "continuous"
threshold = 64
range = [0.25, 4.0]

[bindings.cc_swell.args]
level = "$midicrt_fill_from_cc$"

[bindings.cc_swell.match]
type = "control_change"
number = 30
port_pattern = "Midi Through*"
```

One `[bindings.<id>]` table per binding. `<id>` is the TOML table key itself
(no separate `id` field on disk) and must be unique — a duplicate
`[bindings.<id>]` header is a TOML syntax error, so the loader never has to
de-duplicate.

### `[bindings.<id>]` fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `action` | string | *(required)* | Any registered action name. Not validated against the registry at parse time (roster-dependent — see §4); missing at load time, the whole binding entry is skipped with a warning. |
| `mode` | string | `"trigger"` | `"trigger"` or `"continuous"` — see §3. |
| `threshold` | int, 0–127 | `64` | Only meaningful for `mode = "trigger"` + `match.type = "control_change"`: the CC value must cross this threshold **upward** (lo→hi edge) to fire. Ignored for `note_on` triggers and for continuous mode, but always present with its default so a hand-built or learned `Binding` never needs mode-specific field omission. |
| `range` | `[lo, hi]` (floats) | `[0.0, 1.0]` | Only meaningful for `mode = "continuous"`: the raw 0–127 CC value is linearly lerped into `[lo, hi]`. `lo > hi` (an "inverted" range) works fine — it's plain interpolation, no special-casing which endpoint is numerically larger. Required (and shape-checked) when `mode = "continuous"`; a missing/malformed `range` on a continuous binding skips the whole entry with a warning. |
| `args` | table | `{}` | Static arguments dispatched verbatim alongside the action. For a continuous binding, exactly one entry must be the **fill sentinel** (see below) — that key gets overwritten with the lerped CC value on every fire; every other entry (both modes) is copied through unchanged. |
| `[bindings.<id>.match]` | table | *(required)* | See below. |

### `[bindings.<id>.match]` fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `type` | string | *(required)* | `"note_on"` or `"control_change"` — the only two MIDI message shapes a binding can watch (mirrors what `bind.learn` can ever capture, §3). |
| `number` | int, 0–127 | *(required)* | Note number (`type = "note_on"`) or controller number (`type = "control_change"`) — `MidiEvent.data1` either way. |
| `channel` | int, 0–15, or absent | `None` (any channel) | **0-indexed**, matching `MidiEvent.channel` directly — NOT the human 1-indexed display the event-log's `summary` strings use. Absent means "any channel." |
| `port_pattern` | string, or absent | `None` (any port) | `fnmatch`-style glob matched against the MIDI event's source port name (e.g. `"Midi Through*"` matches any port whose name starts with that string). A `bind.learn` capture always writes the **exact, verbatim** source port string — never a wildcard — so a learned binding only fires from the literal physical port it was learned on. Hand-editing to a glob (as in the `cc_swell` example above) is how you make a binding port-agnostic. |

### The fill sentinel (`args` ↔ TOML translation)

A continuous binding's `args` needs a marker for "this key gets filled from
the live MIDI value." In memory (`Binding.args`), that marker is a literal
Python `None`. **TOML has no null literal**, so it cannot round-trip
verbatim — the on-disk stand-in is the reserved string
`$midicrt_fill_from_cc$` (`bindings.py::CONTINUOUS_FILL_TOKEN`).
`BindingsFile` translates `None` ↔ the token exactly at the load/save
boundary; nothing else in the engine (the dispatcher, `validate_binding`,
`bind.learn`) ever sees the on-disk string. This translation is scoped
**strictly to `mode = "continuous"`** — a trigger binding's `args` are
copied through completely verbatim in both directions, even if one
happened to contain that exact literal string as a genuine static value
(an extremely unlikely collision, but a real one: earlier in Phase 4 the
substitution ran unconditionally and silently corrupted exactly this case).
Practical upshot: an action that genuinely needs the literal string
`"$midicrt_fill_from_cc$"` as a static arg cannot be bound in continuous
mode — no shipped action does.

### Machine-managed header + write discipline

Every `bindings.toml` save writes the header comment shown at the top of
the example above. `bindings.toml` is the **only** config file the engine
ever writes, and only from two call sites: a `bind.learn` capture and
`bind.remove`. Each save is atomic (temp file in the same directory +
`os.replace`) and rewrites the **entire** file from the engine's in-memory
binding list — hand edits made between saves survive as data (re-parsed
normally on the next load), but comments/formatting you added do not: a
plain, dependency-free TOML serializer has no comment-preservation story.
Hand-editing is fully supported (the loader has no idea whether an entry
came from a human or from `bind.learn`) — just expect the next `bind
learn`/`bind remove` to flatten your formatting.

Tolerant of unknown keys at every level (a stray key inside a binding's
table or its `match` sub-table is silently ignored, matching
`config.toml`'s own convention) and of a missing file entirely (no file =
no bindings, no built-in defaults the way `keymap.toml` has
`DEFAULT_KEYMAP` — a binding only ever comes from an explicit `bind.learn`
or a hand-written entry). A genuinely malformed file (bad TOML syntax, or
`bindings` not a table) never crashes the daemon or tears down a
`config.reload` connection — same `load_or_warn` discipline as keymap.toml
(§1's "Malformed-file resilience"), with the previous **last-good**
`BindingsFile` kept on a reload failure.

---

## 3. Trigger vs continuous — and why continuous wants an *absolute* setter

Every binding is one of two modes:

- **`trigger`** fires ONCE per qualifying edge, then does nothing until the
  next one:
  - `note_on`: any note-on with velocity > 0 (a velocity-0 note-on is a
    running-status note-off, not a real trigger).
  - `control_change`: the CC value crossing `threshold` **upward**
    (lo→hi), tracked per `(binding, source, channel)`. A binding's very
    first CC message ever seen only establishes the baseline and **never
    fires**, however high its value already is — this is deliberate: an
    already-high physical fader must not spuriously fire the instant the
    daemon boots or a `config.reload` runs, before anyone touched it.
- **`continuous`** fires on **every** qualifying `control_change` — no edge
  concept at all. The raw 0–127 value is lerped into `range` and fills the
  one `args` entry marked with the fill sentinel; every other `args` entry
  is a static value passed through unchanged.

**Continuous-mode targets need an absolute setter, not a delta/increment
one.** A continuous binding fires once per *physical move* of the
controller, and each firing carries the CC's current lerped value — if the
bound action *adds* that value to its existing state (a `{"delta": float}`
shape), each firing keeps adding on top of whatever the previous firing
already added, saturating to the action's own clamp almost immediately
regardless of the knob's actual position. `pianoroll.zoom {delta: float}`
is exactly this shape (a cumulative nudge, correct for a keymap/CLI
one-shot or a `trigger`-mode binding) — it is the **wrong** target for
`continuous` mode. `pianoroll.zoom_level {level: float}` is the absolute
counterpart built specifically as a continuous target: it sets the zoom to
*exactly* the given value every call, clamped to `[0.25, 4.0]`, never
reading or adding to the current zoom. `validate_binding` cannot detect
this distinction from the schema alone (both shapes declare one `"float"`
arg) — **this is a documented convention, not a mechanically enforced
one.** When binding (or learning) a fader/knob to a continuous parameter,
look for the action's own docstring/description calling out "absolute" vs
"cumulative delta" before picking a target.

Live-verified (Task 4 smoke, §5 below): a learned continuous binding on
`pianoroll.zoom_level` reproduces CC=0/64/127 → zoom exactly 0.25 /
0.5039370078740157 (`64/127`) / 1.0, **unaffected by a prior 33-message
sweep across the full CC range** — proving the knob's position, not its
history, is what determines the parameter.

---

## 4. Learn usage

### CLI

```
midicrt bind learn <action> [--mode trigger|continuous] [--arg k=v ...] [--timeout N]
midicrt bind list
midicrt bind remove <id>
midicrt bind cancel
```

- **`bind learn <action>`** arms the engine's single learn slot, then
  blocks waiting for the resulting `learn_bound`/`learn_cancelled` event
  (no `subscribe` needed — the engine broadcasts every event to every
  greeted client unconditionally). Prints the new binding's JSON on
  success (exit 0), or the cancellation/timeout reason (exit 1).
  `--timeout` is the **client's** own wait timeout (default: the engine's
  30s arm window + 5s slack); it does not change how long the engine
  itself stays armed.
- **`--mode continuous`**: the target action's schema must declare
  **exactly one** `"float"` arg — that arg is auto-detected as the fill
  target; you must NOT also pass it via `--arg` (there's nothing left to
  statically supply for a key learn is about to fill from live MIDI). If
  the action declares zero or more than one float arg, the arm is
  rejected immediately with a clear error naming the count, before any
  MIDI port is even touched. Continuous learns always use `range = [0.0,
  1.0]` — there is no `--range` flag; a custom range needs a `bind remove`
  + hand-edit, or a fresh learn followed by editing `bindings.toml`
  directly.
- **`--arg k=v`**: fills every *other* (non-float, non-fill) argument the
  target action's schema wants. Values are always sent as strings (the
  generic `--arg` convention); the wire-level `bind.learn` action itself
  accepts a real nested JSON object too if you're driving it via `midicrt
  action bind.learn --arg args='{"name":"pianoroll"}'` directly.
- **`bind list`**: every persisted binding, each with a live `valid`/`error`
  pair (see §6).
- **`bind remove <id>`**: drops a binding by id (from `bind list`'s own
  output), persists, and disarms the live dispatcher immediately — a
  removed binding cannot fire again even for a MIDI event already in
  flight when the remove happens.
- **`bind cancel`**: disarms the current learn slot if one is armed;
  harmless no-op (not an error) if nothing is armed.

### What qualifies as a capture

Only `control_change` (any value — a fader sitting anywhere still
identifies which controller it is) or `note_on` with velocity > 0 qualify.
Everything else the engine ever sees — `clock_tick` (synthesized, no
controller identity), `note_off`, transport `start`/`stop`/`continue`/
`songpos`, `sysex`, `program_change` — is disqualified and simply ignored
while armed; nothing captures it.

### Validation happens entirely at arm time, never at capture

`bind.learn` validates the action name, mode, and (for continuous) the
float-arg auto-detection **before** ever arming — a human at the CLI sees
a rejection immediately, in response to the command that caused it, rather
than only discovering a problem later in the daemon log whenever a MIDI
event happens to land.

### Replace-on-relearn

Re-learning a control that's already bound **replaces** the old binding —
this is standard DAW behavior, and the only behavior consistent with
"learn" meaning *assign* rather than *add*. Specifically: when a capture
completes, the engine removes every currently-persisted binding whose
`match` is **exactly equal** (type, number, channel, and port_pattern all
identical) to the one just captured, adds the new binding, and saves once
— the file goes directly from "old binding present" to "new binding
present," never a state with both or neither. `bind learn`'s CLI output
prints a `midicrt: replaced binding <id> (<action>)` line per replaced
entry, above the new binding's JSON, whenever this happens.

**Wildcard bindings are never replaced by an exact-match relearn.** The
replace check is exact-match only — a pre-existing binding with
`port_pattern = "Midi Through*"` is left untouched even if the freshly
learned exact-port binding would now *also* fire on the same physical
event going forward, so the two coexist. This is deliberate: there is no
single unambiguous "the one being replaced" once wildcard overlap is in
play (several wildcard bindings could each match different physical events
through the same pattern), whereas an exact match has exactly one obvious
candidate. If you need to replace a wildcard binding, remove it explicitly
with `bind remove` first.

### Timeout

An armed learn slot auto-cancels after 30 seconds with no qualifying event
(`learn_cancelled {"reason": "timeout"}`, checked once per engine tick — no
extra timer machinery). A second `bind.learn` call while already armed just
replaces the arm outright and re-emits `learn_armed` — no stacking, no
"previous arm discarded" event (the old arm was never observable as
anything richer than "armed" in the first place).

### Worked examples (from this task's live smoke — see the task-4 report for full transcripts)

Trigger, page navigation, zero clients attached:

```
$ midicrt bind learn page.next --timeout 20 &
$ <inject note 60 via Midi Through>                 # captures
$ <inject note 60 again>                            # real fire
$ midicrt status                                    # page: eventlog -> voices

$ midicrt bind learn page.prev --timeout 20 &       # RELEARN same note
$ <inject note 60 via Midi Through>                 # captures + replaces
midicrt: replaced binding learn_1786140583127 (page.next)
$ <inject note 60 again>                            # real fire
$ midicrt status                                    # page: voices -> eventlog (back)
```

Continuous, an absolute setter, proven proportional under load:

```
$ midicrt bind learn pianoroll.zoom_level --mode continuous --timeout 20 &
$ <inject one CC 20 message via Midi Through>       # captures
$ <inject a 33-message CC sweep across the full 0-127 range>   # no effect on the proof below
$ <inject CC 20 = 0>   -> pianoroll zoom = 0.25
$ <inject CC 20 = 64>  -> pianoroll zoom = 0.5039370078740157   (== 64/127 exactly)
$ <inject CC 20 = 127> -> pianoroll zoom = 1.0
```

---

## 5. Roster-dependence caveats

The action registry is built once, at engine startup, from `config.pages`
— any page not in that list never registers its actions at all
(`pianoroll.*`, `sendnotes.key`, etc.). This means:

- A binding or keymap entry naming an action from a **larger** build (or a
  previous `config.toml`) than the one currently running is not an error —
  it's simply invalid *for this build*, reported as such (never dropped
  silently for bindings; dropped with a log line for keymap entries — see
  §6 for why the two layers disagree on disposition).
- There is **no live roster migration**. `config.reload` re-reads
  `keymap.toml` and `bindings.toml` unconditionally, but only **warns**
  (never applies) if `config.toml`'s `pages` list itself changed — the
  page/analyzer graph (harmony's shared-instance dedup, sendnotes' real
  MIDI output, per-topic subscriber refcounts) has no tested path for
  being re-parented while running. A roster change needs a full
  `systemctl restart midicrtd`.
- Because of this, a `bindings.toml`/`keymap.toml` written against one
  `config.toml` may need re-validation after any change to the `pages`
  list — `bind list` (bindings) and the daemon log (keymap, at the next
  boot or reload) are how you find out.

---

## 6. Troubleshooting

**A binding never fires.** Run `midicrt bind list` and check its `valid`/
`error` fields — bindings are **kept, not dropped**, when invalid (unlike
an unbindable keymap entry, which is silently filtered out of `describe`'s
`keymap`). This is deliberate: a binding can go stale *after* being saved
(hand-editing, or a smaller page roster on the next boot), and `bind list`
needs something to actually show as broken rather than have it vanish with
no trail. Common `error` values: `unknown action: '...'` (the action isn't
in this build's roster at all), `missing args for '...': [...]` / `unknown
args for '...': [...]` (the `args` table doesn't match the action's
schema), and for continuous bindings, `fill arg '...' is not declared
'float' by action '...'` or a wrong fill-marker count. A binding that
*is* reported `valid` but still never seems to fire is almost always a
`match` mismatch — double check `channel` (0-indexed!) and `port_pattern`
(exact string unless you hand-edited it to a glob) against the real
incoming event.

**`config.reload` (or a daemon restart) logs a warning and keeps the old
keymap/bindings.** Check `journalctl -u midicrtd` for the exact message —
both files use the identical "never raise, report a warning, keep the
last-good value" discipline for a genuinely malformed file (bad TOML
syntax, or the wrong shape at a known key). The connection issuing
`config.reload` stays alive either way; the returned JSON's `warnings`
list carries the same message the log does.

**A keymap entry silently doesn't do anything.** It was filtered out at
`filter_known_actions` time — check `journalctl -u midicrt` for a line
naming the key and the reason (unknown action, or an action that requires
args a single keypress can't supply). Six shipping actions always fall
into the latter bucket (§1) — bind those via `bind.learn`/`bindings.toml`
instead, which *can* carry static `args`.

**`bind.learn` rejects the arm immediately.** The error names the exact
problem (unknown action, wrong `mode` value, wrong float-arg count for
continuous) — this is by design: arm-time validation exists specifically
so a bad `bind learn` call fails at the command that caused it, not later
against whatever MIDI event happens to arrive.

**A relearn didn't replace what you expected.** Replace-on-relearn is
exact-match only (§4) — if the binding you meant to replace has a
`port_pattern` glob (not the exact port string a real `bind.learn` capture
always writes), it will not be touched by a new exact-match capture.
`bind remove` it explicitly first.
