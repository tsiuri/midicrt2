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

## 1b. Schema v2 (Phase 8 Task 6) — per-page sections, args-table entries,
## roster-positional jumps, the help overlay

Added at the 2026-08-08 keymap revamp (docs/gui-phase-decisions-2026-08-08.md
on motherbase; full design rationale in `engine/keymap.py`'s own module
docstring and `.superpowers/sdd/2026-08-08-midicrt2-phase8-gui/task-6-
report.md`). Purely additive over §1 above — every schema-v1 `keymap.toml`
still loads and behaves identically; nothing here changes v1's own
contract.

```toml
[keys]
"v" = "eventlog.clear"
"1" = {action = "page.jump", args = {position = 1}}   # example override

[keys.pianoroll]
"[" = {action = "pianoroll.zoom", args = {delta = -0.1}}
"p" = "pianoroll.projection_toggle"
```

**Two entry shapes.** A `[keys]` (or `[keys.<page>]`) value is either the
familiar plain **string** action name (§1, unchanged — only valid for an
ARGLESS action), or an **args-table**: `{action = "name", args = {...}}`.
The args-table's `args` must supply **exactly** the target action's full
schema — every arg the action needs, no extras — checked by
`filter_known_actions` at the same registry-boundary timing as §1's own
validation (dropped + logged, never raised, on a mismatch). This is what
makes an args-requiring action bindable to a single keypress at all: §1's
"six shipping actions" that could never be keymap-bound (`sendnotes.key`,
`page.goto`, `pianoroll.zoom`/`.projection`/`.channels`, `pagecycle.
enable`) all CAN be now, via an args-table entry that pins one specific
argument value to that key — a bare STRING binding to one of them is still
rejected exactly as §1 describes; only the args-table shape closes that
gap.

**Per-page sections, `[keys.<page>]`.** A nested table under `[keys]`,
keyed by page name, holding entries scoped to when that page is CURRENT —
merged OVER the global `[keys]` table (a page-scoped key wins over a
global one bound to the same character) whenever that page is displayed,
and simply absent from dispatch on every other page. Same merge-not-
replace semantics as §1's global table: a `[keys.pianoroll]` overriding
one key leaves every OTHER pianoroll default (and every global default)
intact — see `engine/keymap.py::DEFAULT_PAGE_KEYMAPS` for the shipped
per-page defaults (pianoroll/img2txtviz/sendnotes/spectrum today).
Discriminated from an args-table entry purely by shape: a `[keys]` dict
value WITHOUT an `"action"` key is a page section; WITH one, it's an
args-table entry for that literal key.

**Roster-positional page jumps (`page.jump`, superseded as a DEFAULT
binding by §1c below).** `page.jump {position: int}` (unlike
`page.goto {name: str}`) resolves against the roster's CURRENT order at
dispatch time — always reachable, roster-shape-independent — and an
out-of-range position is a **silent no-op**, logged at INFO, never an
`ActionError` — deliberately different from `page.goto`'s loud failure on
an unknown NAME, since a typo'd name is a real mistake but an unmapped
position number on a smaller roster is entirely expected. The action
itself is unchanged and still fully dispatchable; `DEFAULT_KEYMAP` no
longer bakes it onto the digit keys (§1c), but any `keymap.toml` can still
bind a key to it directly (`"v" = {action = "page.jump", args =
{position = 5}}`) for genuinely roster-relative behavior no fixed-ID
scheme can express.

**On-screen keymap indicator + help overlay** (the other half of the
Phase 8 revamp, no `keymap.toml` schema of their own — mentioned here
since both read straight off the tables above): a chrome element shows the
CURRENT page's own `[keys.<page>]` hints compactly (toggle: `config.toml`'s
`keymap_hints_enabled`, default `true`); `?` (a `DEFAULT_KEYMAP`
pseudo-action, `client.help_toggle`, resolved entirely client-side like
`client.quit`) opens/closes a dim panel listing the GLOBAL section then
the current page's own section — any key dismisses it (swallowed, never
reaching the engine) — without switching pages or touching the underlying
subscription. `midicrt-fb --out PATH --overlay` renders one frame with the
panel forced on, for headless verification with no interactive keyboard.
A `page.goto` entry with a v1 page ID resolves to `"-> {id}:{TITLE}"` in
this overlay (`"-> 8:PIANOROLL"`, §1c below) — the same text the marquee's
own `[8:PIANOROLL]` header shows for that page.

---

## 1c. Digit navigation: v1 page-ID based (Phase 9 Task 0)

Added at the 2026-08-09 reconciliation (docs/gui-phase-decisions-
2026-08-08.md's "Phase 8 CLOSED" section on motherbase; full design
rationale in `engine/keymap.py`'s own module docstring and
`.superpowers/sdd/2026-08-09-midicrt2-phase9-instruments/task-0-
report.md`). §1b shipped roster-POSITIONAL digit jumps (`page.jump`,
above); this task found that scheme clashed with `analyzers/marquee.py`'s
header text, which shows v1's OWN page-ID vocabulary (`[8:PIANOROLL]`) —
pressing "8" jumped to the 8th roster position (a DIFFERENT page,
"config", on the stock roster), not the page the marquee's own "8"
names. Resolved as a controller ruling: **digits now map to v1 IDs**, not
roster position.

`DEFAULT_KEYMAP`'s digit/shifted-digit bindings are regenerated from
`analyzers/marquee.py::PAGE_IDS` (the marquee's own single source of
truth for "v1 page ID ↔ v2 page name") as `page.goto {name: "..."}`
args-table entries — reusing `page.goto` exactly as it already exists, no
new action. One binding per `PAGE_IDS` entry (14 today), regardless of
whether that page is in any particular build's roster:

| v1 ID | key | page | v1 ID | key | page |
|---|---|---|---|---|---|
| 0 | `0` | help | 9 | `9` | spectrum |
| 1 | `1` | harmony | 10 | `)` | tuner *(not in default roster)* |
| 2 | `2` | sendnotes | 11 | `!` | chordkey |
| 4 | `4` | ccmonitor | 13 | `#` | voices |
| 5 | `5` | ccdashboard | 14 | `$` | config |
| 6 | `6` | eventlog | 17 | `&` | img2txtviz |
| 7 | `7` | progchanges | | | |
| 8 | `8` | pianoroll | | | |

(v1 IDs 3, 12, 15, 16, 18, 19 have no current page — those keys/shift-keys
are simply absent from `DEFAULT_KEYMAP`, not bound to a no-op.)

**Formula** (verified against v1's own real keymap, `docs/visual-audit.md`'s
"Global page-switch keymap" audit row, not re-derived): unshifted digit
character = the ID's ones digit for IDs 0–9; for IDs 10–19, the SHIFTED
variant of that same ones-digit character (`"1"`→`"!"`, `"3"`→`"#"`, …,
using the same `DIGIT_ROW`/`SHIFTED_DIGIT_ROW` pairing the fb client's own
evdev shifted-char table already keys off of) — e.g. ID 11 → ones digit
`"1"` → shifted `"!"`. This reproduces v1's own `!`→11 … `&`→17 shifted-row
scheme exactly, with one disclosed deviation: v1 reaches page 10 ("tuner")
via a dedicated `t`/`T` letter key instead of a digit at all; this scheme
does not special-case it (letters are reserved for per-page functions
under this revamp's own ruling) — tuner is reachable via shift+0 (`")"`)
instead, same formula as every other ID.

**Roster pages with no v1 ID** (`"screensaver"` today,
`analyzers/marquee.py`'s own docstring) get **no** digit binding at all —
there's no ID to derive one from.

**Graceful no-op for a known-but-absent page.** A `page.goto` target that
HAS a v1 ID but isn't in THIS build's roster (e.g. "tuner" on the stock
default roster) is a logged, silent no-op — `Engine._page_goto`
(`engine/core.py`) narrows its own "unknown page" `ActionError` to this
exact case, the same "ordinary, expected situation" category `page.jump`'s
out-of-range position already established. A name with no v1 ID at all
(a genuine typo) still raises loudly, unchanged.

Cleanly reversible: `page.jump` (§1b) is completely untouched, so a
`keymap.toml` can restore roster-positional digits at any time by
overriding each key back to a `page.jump {position: N}` entry.

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
port_pattern = "Midi Through:Midi Through Port-0*"   # suffix-globbed, see §2's port_pattern row
device = "virt:Midi Through:Midi Through Port-0"     # PRIMARY when present -- see §2's device row

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
| `port_pattern` | string, or absent | `None` (any port) | `fnmatch`-style glob matched against the MIDI event's source port name (e.g. `"Midi Through*"` matches any port whose name starts with that string). **Updated, Phase 5 Task 3 (docs/phase5-capture.md):** a `bind.learn` capture no longer writes the exact, verbatim source string — it strips the trailing volatile ALSA `<client>:<port>` numbering suffix and appends `*` (`engine/bindings.py::glob_port_pattern`), so a learned binding survives that suffix renumbering across a reboot/replug/rtpmidid session restart instead of silently going dead. See docs/phase5-capture.md's "Learned-binding port durability" section for the full rationale and the exact transform. **Superseded as the primary match key, Phase 9 Task 1 (docs/phase5-capture.md §7):** ignored entirely once `device` (below) is also present on the same binding — see that row. |
| `device` | string, or absent | `None` (no device constraint) | **Added, Phase 9 Task 1** (device-identity bindings). A stable device-identity string from `engine/midi_identity.py::IdentityResolver`'s resolution ladder — `usb:<vendor>:<product>:<serial>` for a USB MIDI interface that exposes a serial number, `usb:<vendor>:<product>` (no serial — see the collision note below) or `virt:<client:port name, suffix-stripped>` otherwise (kernel virtual ports like `Midi Through`, rtpmidid's network-export ports, or any non-USB kernel card). **When present, `device` is the SOLE port-identity check — `port_pattern` is not also consulted (not ANDed, not ORed).** `bind.learn` always writes BOTH fields going forward: `device` from the capturing event's resolved identity, `port_pattern` from the pre-existing suffix-globbing — so `port_pattern` survives on disk as an inert, hand-editable fallback (delete the `device` line to revert one binding to pure pattern matching). A bindings.toml with no `device` key at all (every file written before this task) is unaffected — matching falls straight through to `port_pattern`, exactly as it always has. |

**Disclosed limitation: identical-device collision — fixed for
serial-bearing devices, Phase 9 Task 1.** Stripping the `<client>:<port>`
suffix (Phase 5 Task 3) bought reboot/replug durability for `port_pattern`
at a real cost: it also stripped the ONE thing that could distinguish two
DIFFERENT physical ports ALSA happens to enumerate under the SAME name,
most commonly two simultaneously-connected units of the same USB MIDI
interface model. **Phase 9 Task 1's `device` field (above) closes this
gap for any USB MIDI interface that exposes a hardware serial number** —
`bind.learn` now captures the device's own `usb:<vendor>:<product>:<serial>`
identity, which is unique per physical unit regardless of which USB port
it's plugged into, so a binding fires for the correct device even after a
replug, and two distinct-serial units of the same model no longer
cross-fire each other's bindings.

**The gap that remains, disclosed, not silently traded away:** a USB MIDI
interface that does NOT expose a serial number (`IdentityResolver`
resolves it to `usb:<vendor>:<product>` with no third segment — a real,
live-probed case: this Pi's own attached USB Audio Device has no `serial`
sysfs attribute at all) is still indistinguishable from a second unit of
the exact same serial-less model — both resolve to the identical
`device_id`, so a binding still cross-fires between them, exactly as it
did under the old pattern-only scheme. `bind list`'s `device_present:
true` does not catch this either (same "checks *some* match, not *exactly
one*" limitation `port_present` always had). If you run multiple
identical-model, serial-less MIDI interfaces at once, hand-edit the
affected binding's `port_pattern` back to an exact string (or `device` to
something else entirely) — see docs/phase5-capture.md §7 for the full,
updated writeup and `engine/midi_identity.py`'s own module docstring for
exactly why a serial can't be invented where the hardware doesn't provide
one.

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
midicrt bind learn <action> [--mode trigger|continuous] [--arg k=v ...] [--timeout N] [--range lo,hi]
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
  MIDI port is even touched. Continuous learns default to `range = [0.0,
  1.0]`.
- **`--range lo,hi`** (Phase 5 Task 3): continuous mode only — overrides
  the default `[0.0, 1.0]` lerp target, e.g. `--range 0.25,4.0` for a
  zoom-style parameter whose useful range doesn't start at 0. Silently
  ignored for `--mode trigger` (a trigger binding has no `range` concept
  to apply it to). A malformed value (not exactly two comma-separated
  floats) is rejected immediately, same arm-time-validation discipline as
  every other `bind learn` check. Wire-level equivalent: `bind.learn`'s
  optional `range: str` arg (omit it entirely for the default — an
  explicit empty string also means "not supplied").
- **`--arg k=v`**: fills every *other* (non-float, non-fill) argument the
  target action's schema wants. Values are always sent as strings (the
  generic `--arg` convention); the wire-level `bind.learn` action itself
  accepts a real nested JSON object too if you're driving it via `midicrt
  action bind.learn --arg args='{"name":"pianoroll"}'` directly.
- **`bind list`**: every persisted binding, each with a live `valid`/`error`
  pair (see §6) plus (Phase 5 Task 3) a `port_present: bool` -- whether
  `match.port_pattern` currently matches any OPEN MIDI input port
  (`fnmatch` against `MidiInput.open_ports`, live at call time). `True`
  when `port_pattern` is `None` (no port constraint) or when no live port
  roster is known at all (`--no-midi`, or a daemon started before
  `Engine.set_open_ports_provider` was wired) -- reported `False` ONLY
  when there genuinely IS a known port roster and it does not contain a
  match. Pure diagnostic sugar (never gates whether the binding actually
  fires; `BindingDispatcher._matches` is untouched) for exactly the
  "double check ... `port_pattern` ... against the real incoming event"
  troubleshooting step §6 already tells a human to do by eyeball.
  **Added, Phase 9 Task 1:** `device: str | None` (the binding's own
  `match.device`, or `null`) and `device_present: bool` -- the exact same
  "any open port resolve to this?" check, against `MidiInput.
  open_device_ids` this time (`Engine.set_open_device_ids_provider`).
  Same `True`-means-"unknown-or-unconstrained" precedent as
  `port_present`; `False` only when a device identity is genuinely known
  and genuinely not currently open.
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
(a suffix-globbed pattern since Phase 5 Task 3, or an exact string if you
hand-edited it — see docs/phase5-capture.md) against the real incoming
event.

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
