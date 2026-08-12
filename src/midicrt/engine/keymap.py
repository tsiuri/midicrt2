"""Config-served keymap -- Phase 4 Task 1 (docs/phase4-notes.md) built the
original single `[keys]` TOML table mapping one-character keys to action
names; Phase 8 Task 6 (docs/gui-phase-decisions-2026-08-08.md's "keymap +
page navigation: full revamp") is the schema-v2 upgrade this module now
serves: per-page `[keys.<page>]` sections merged OVER the global `[keys]`
table when that page is current, roster-POSITIONAL number-key page jumps,
and keymap entries that carry their own baked-in `args` (so an
args-requiring action CAN be bound to a single keypress, provided the
keymap entry fully specifies every arg the action's schema needs).

Both clients (`clients/tui.py`, `clients/fb/app.py`) still build their key
dispatch from whatever the engine serves over `describe` at connect
(`clients/base.py::dispatch_key`/`fetch_keymap`/`fetch_keymap_sections`).

Schema v2: two entry shapes, one discriminator
-------------------------------------------------------------------------
A `[keys]` (or `[keys.<page>]`) entry is either:
  - a plain **string** action name (unchanged from Phase 4 -- "the action
    takes no args, or the client can never supply one anyway"), or
  - an **args-table**: `{action = "name", args = {...}}` -- the SUPPLIED
    args are baked into the keymap entry itself, not read from anywhere
    at dispatch time. This is what makes `page.jump {position: int}` (and
    any other args-requiring action a keymap author wants to pin to one
    specific args value) bindable to a single keypress at all.

A `[keys.<page>]` sub-table is distinguished from an args-table entry by
the SAME "action" key: a dict value under `[keys]` that has an `"action"`
key is an args-table entry for that literal key; a dict value that does
NOT have an `"action"` key is a nested PER-PAGE section (TOML naturally
produces exactly this shape: `[keys.pianoroll]` parses to
`raw["keys"]["pianoroll"] = {...}`). This keeps the schema to one flat
`[keys]` namespace plus one level of page-scoped nesting -- no second
discriminator field, no separate top-level table.

Why `page.jump {position: int}`, not `page.goto {name: "..."}`, for number
keys -- SUPERSEDED for `DEFAULT_KEYMAP` by Phase 9 Task 0, see the next
section
-------------------------------------------------------------------------
The original (Phase 8 Task 6) ruling asked for numeric keys to jump by
ROSTER POSITION ("1st page", "5th page"), not by a fixed page NAME -- a
deploy's `config.pages` order can differ (or change), so "5" must always
mean "whatever page is 5th in THIS build's roster" rather than a name
baked in at some other build's roster shape. Two designs were on the
table:
  1. Bake `page.goto {name: "<resolved-name>"}` into `DEFAULT_KEYMAP` at
     `Engine.__init__` time, once the real roster is known -- rejected:
     `DEFAULT_KEYMAP` is a plain module-level constant read by
     `load_keymap`/tests/other call sites BEFORE any `Engine` exists; it
     cannot depend on a roster that doesn't exist yet at that point.
  2. A new roster-relative action, `page.jump {position: int}`, resolved
     against `Engine.pages` at DISPATCH time (`Engine._page_jump` below) --
     chosen. `DEFAULT_KEYMAP` stays a static, roster-independent constant
     (position numbers, not names), and the SAME args-table mechanism this
     task adds for arbitrary per-page bindings covers it for free -- no
     separate binding mechanism needed just for number keys.
`page.jump`'s out-of-range behavior is deliberately DIFFERENT from
`page.goto`'s: a typo'd `page.goto` NAME is a real user mistake and should
raise loudly (`ActionError`, unchanged); a `page.jump` POSITION beyond the
current roster's length is an ordinary, expected situation and is a
silent, logged no-op instead (`engine/core.py::Engine._page_jump`'s own
docstring).

`page.jump` itself is UNCHANGED and stays fully registered/dispatchable --
this action's own docstring (still accurate) is preserved above/below for
that reason. What Task 0 supersedes is only which action `DEFAULT_KEYMAP`
itself bakes onto the digit keys -- a hand-written `keymap.toml` can still
bind any key to `page.jump {position: N}` for genuinely roster-relative
behavior no v1-ID scheme could ever express (v1 has no roster-relative
concept at all -- every one of its own keys names a fixed, absolute page
ID).

Digit navigation: v1 page-ID based, not roster-positional (Phase 9 Task 0
-- docs/gui-phase-decisions-2026-08-08.md "Phase 8 CLOSED" section)
-------------------------------------------------------------------------
Phase 8 Task 6 shipped the roster-positional scheme above and its own
`analyzers/marquee.py::MarqueeAnalyzer` (built the SAME task) exposed a
clash neither task noticed at the time: the header marquee shows v1's OWN
page-ID vocabulary (`[8:PIANOROLL]`, `PAGE_IDS`), but pressing "8" jumped
to the 8th ROSTER position -- "config" on the stock default roster, a
DIFFERENT page than the marquee's own "8" names. Phase 8's own closeout
doc flagged this as an open, blocking ruling ("(a) marquee shows
positions, or (b) digits map to v1 IDs -- user decides"). This task
resolves it as (b), a controller ruling disclosed in that doc and this
task's own report -- cleanly reversible, since `page.jump` (above) is
untouched and a keymap.toml can restore roster-positional digits at any
time by overriding each key back to a `page.jump` entry.

The rejection reason for design #1 above (`DEFAULT_KEYMAP` can't depend on
"the real roster" -- it doesn't exist at module-load time) does NOT apply
here: `analyzers/marquee.py::PAGE_IDS` is a DIFFERENT table from "the real
roster" -- v1's own FIXED page-ID vocabulary, a plain module-level
constant that exists before any `Engine` does, no different in kind from
`DIGIT_ROW`/`SHIFTED_DIGIT_ROW` below. `DEFAULT_KEYMAP`'s digit bindings
are therefore baked directly FROM `PAGE_IDS` at THIS module's own
load time (`_default_v1_id_goto_bindings` below), using `page.goto
{name: "<the PAGE_IDS name>"}` entries -- reusing `page.goto` exactly as
it already exists (no new action), per this task's own brief.

Digit <-> v1-ID mapping formula (verified against v1's OWN real keymap --
docs/visual-audit.md's "Global page-switch keymap" audit row, not
re-derived from scratch)
-------------------------------------------------------------------------
That audit row reads v1's real keyboard dispatch as: unshifted `"0"`-`"9"`
jump DIRECTLY to v1 page IDs 0-9; shifted `"!@#$%^&*"` (shift+1..shift+8)
jump to IDs 11-17 SEQUENTIALLY (`!`->11, `@`->12, ..., `&`->17) -- "a
deliberate non-literal mnemonic scheme" per that row, since shift+1
landing on ID 11 (not ID 1) only makes sense once you know the shifted row
starts counting at 11, not at the shifted key's own face value.
`_default_v1_id_goto_bindings` below reproduces this exactly via ONE
formula spanning the full 0-19 ID space (today's `PAGE_IDS` values top out
at 17): the unshifted digit CHARACTER equals the ID's ones digit for IDs
0-9; for IDs 10-19, the SAME ones-digit character is used, but its SHIFTED
variant (`_SHIFT_OF`, the same `DIGIT_ROW`/`SHIFTED_DIGIT_ROW` pairing
`clients/fb/app.py::_build_evdev_shifted_char_table` already keys off of)
-- e.g. `id=11 -> ones=1 -> shift_of["1"] == "!"`, ..., `id=17 -> ones=7 ->
shift_of["7"] == "&"`, reproducing v1's own table character-for-character
with no second, hand-typed lookup.

Disclosed deviation -- v1 page ID 10 ("tuner"): v1 does NOT reach page 10
through this digit/shift scheme at all -- it binds a dedicated letter key
(`t`/`T`) instead, an ad hoc exception to its own numbering (same audit
row). This formula does NOT special-case it: `id=10 -> ones=0 ->
shift_of["0"] == ")"` (shift+0), the same single rule every other ID
follows. Deliberate, disclosed, for two reasons: (a) letters are reserved
for PER-PAGE functions under this whole revamp's own ruling ("number keys
... for page jumps; letters for per-page functions",
docs/gui-phase-decisions-2026-08-08.md) -- baking one global letter-keyed
page jump back in would itself be a smaller version of the exact
inconsistency this task exists to remove; (b) "tuner" isn't in the default
roster anyway (`config.py`), so whichever key reaches it hits
`Engine._page_goto`'s graceful no-op (engine/core.py, this task's own
addition) on a stock deploy regardless -- see that method's own docstring.
A future config that DOES add "tuner" reaches it via shift+0 (`")"`), not
`t`/`T`; changing that mapping later is a one-line `PAGE_IDS`-adjacent fix,
not a redesign.

Every `PAGE_IDS` entry gets a binding this way UNCONDITIONALLY, regardless
of whether its page is in any particular build's roster -- see
`_default_v1_id_goto_bindings`'s own docstring for why that's safe. A
roster page with NO v1 ID at all (only "screensaver" today, see
`analyzers/marquee.py`'s own docstring) gets no digit binding of any kind
-- there is no ID to derive one from, and no key is reserved for it either
(a hand-written keymap.toml remains free to bind any key to
`page.goto {name: "screensaver"}` directly).

Modifier handling is CLIENT-SPECIFIC, not engine-side (both clients read
the very same keymap table; only how each one PRODUCES the string "!" for
a physical shift+1 keystroke differs):
  - **TUI** (`clients/tui.py`, via `blessed.Terminal.inkey()`): the
    terminal driver itself already resolves a real keyboard's shift+1
    keystroke to the literal character "!" before this process ever sees
    it -- no client-side modifier tracking needed at all, the SAME
    zero-effort path every other printable key already goes through.
  - **fb** (`clients/fb/app.py`, raw `evdev` keycodes): evdev reports
    keyCODES (`KEY_1`, `KEY_LEFTSHIFT`, ...), not resolved characters --
    there is no terminal driver in between to do the shift-resolution for
    it. `_input_loop` tracks `KEY_LEFTSHIFT`/`KEY_RIGHTSHIFT` down/up state
    itself and looks up the down-event's keycode in a SEPARATE shifted-char
    table (`_build_evdev_shifted_char_table`) instead of the plain one
    whenever either shift key is currently held -- see that module's own
    docstring for the two-table/shift-state design.

Default-keymap reality check (verified against both clients' ACTUAL
current source at task-1 start, not assumed) -- UNCHANGED from Phase 4,
restated here since `DEFAULT_KEYMAP` below still carries it forward
-------------------------------------------------------------------------
  - TUI (`clients/tui.py`): `q` quit LOCALLY -- a dedicated
    `if key == "q": return 0` branch that ran BEFORE the old `_KEY_ACTIONS`
    dict was even consulted, so it was never a dispatched action at all.
    `_KEY_ACTIONS = {"c": "eventlog.clear", "n": "page.next"}`. No "p"
    binding existed.
  - fb (`clients/fb/app.py::_input_loop`): `KEY_Q` set `quit_event`
    locally; `KEY_C` sent `eventlog.clear`; `KEY_N` sent `page.next`. No
    `KEY_P` branch existed either.
Both clients therefore exposed the EXACT SAME three real keys, still true
today -- `page.jump`/`client.help_toggle` are the only ADDITIONS this task
makes to the global default table.

Pseudo-actions
--------------
`client.*`-prefixed action names never reach the engine's `ActionRegistry`
at all -- they are handled entirely client-side. `CLIENT_QUIT_ACTION`
("client.quit") and `CLIENT_HELP_TOGGLE_ACTION` ("client.help_toggle",
Phase 8 Task 6) are the two names either client's `dispatch_key`
(`clients/base.py`) gives real meaning to; any OTHER `client.*` value is
treated as an unrecognized (but still harmless -- never sent to the
engine) client-local no-op there. `filter_known_actions` below passes ANY
`client.*` entry (string-shaped only -- an args-table entry naming a
`client.*` "action" is a malformed binding, see that function's own
docstring) through untouched without checking it against the engine's
real action registry -- there is nothing there to check it against, by
design.

Merge semantics (deliberate departure from config.py's own convention)
-------------------------------------------------------------------------
`config.py::load()` replaces each Config FIELD wholesale when a
config.toml supplies it. `load_keymap`/`load_page_keymaps` below do NOT
mirror that for the `[keys]` table (or a `[keys.<page>]` sub-table): a
present file's entries are MERGED on top of `DEFAULT_KEYMAP`/`DEFAULT_
PAGE_KEYMAPS`, key by key -- so a keymap.toml containing only
`v = "eventlog.clear"` still leaves "q"/"c"/"n"/the number-key jumps bound
to their built-in defaults, and a `[keys.pianoroll]` overriding one key
still leaves that page's OTHER default bindings (and every global default)
intact. This mirrors how most keybinding-override systems (editor
`keybindings.json` files, window-manager configs) work in practice --
override what you name, keep everything else.

Validation timing (fail gracefully at dispatch, never at load)
-------------------------------------------------------------------------
`load_keymap`/`load_page_keymaps` (pure TOML parsing + merge) never
validate action NAMES against the engine's real registry -- they have no
registry to check against, and the registry itself is roster-dependent
(some actions like `sendnotes.key` only exist when their owning page is in
`config.pages`). `filter_known_actions` is the separate, second step that
DOES have a registry to check against -- `Engine.__init__` (engine/core.py)
calls it (once for the global table, once per page section) right after
every action has been registered, so a stale keymap.toml entry is logged
and dropped at ENGINE CONSTRUCTION time, never at TOML-parse time.

Malformed-file resilience (bindings review, live-reproduced Critical
finding, still enforced under schema v2)
-------------------------------------------------------------------------
`load_keymap`/`load_page_keymaps` still RAISE on a syntactically invalid
TOML file (`tomllib.TOMLDecodeError`, a `ValueError` subclass) or a
wrong-SHAPED (but syntactically valid) `[keys]` table -- both stay small,
pure, raising functions, matching `config.py::load()`'s own precedent.
`load_keymap_or_warn`/`load_page_keymaps_or_warn` are the separate safe
wrappers `engine/core.py`'s two production call sites (`__init__` and
`_config_reload`) actually use -- a bad optional config file must never
crash daemon startup nor tear down a `config.reload` requester's
connection; see those functions' own docstrings for the full incident.
"""
import logging
import os
import tomllib
from typing import Any

from midicrt.analyzers.marquee import PAGE_IDS as _V1_PAGE_IDS

_LOG = logging.getLogger(__name__)

DEFAULT_PATH = os.path.expanduser("~/.config/midicrt/keymap.toml")

# Client-local pseudo-actions: never dispatched to the engine (see module
# docstring's "Pseudo-actions" section). `clients/base.py::dispatch_key` is
# the one place that recognizes these literal strings.
CLIENT_QUIT_ACTION = "client.quit"
CLIENT_HELP_TOGGLE_ACTION = "client.help_toggle"

# The roster-positional jump action (module docstring's "Why page.jump"
# section) -- registered unconditionally by `engine/core.py::Engine.__init__`
# (like `page.next`/`page.prev`/`page.goto`), resolved against the live
# roster at DISPATCH time, never at keymap-load time.
PAGE_JUMP_ACTION = "page.jump"

# Standard US-keyboard shift-row layout: shift+1..shift+0 -> these symbols,
# in the SAME left-to-right order as "1234567890". Used both by the
# v1-ID digit/shift formula below AND by fb's evdev shifted-char table
# (`clients/fb/app.py::_build_evdev_shifted_char_table`) so the two stay
# derived from one source of truth, not two hand-typed copies of the same
# layout.
DIGIT_ROW = "1234567890"
SHIFTED_DIGIT_ROW = "!@#$%^&*()"

# `_SHIFT_OF["3"] == "#"`, etc -- the SAME positional pairing DIGIT_ROW/
# SHIFTED_DIGIT_ROW already encode (index i of one <-> index i of the
# other), as a lookup instead of a linear scan. Used by
# `_default_v1_id_goto_bindings` below to find "the shifted key that types
# the same digit character" for any v1 ID >= 10 -- see module docstring's
# "Digit <-> v1-ID mapping formula" section.
_SHIFT_OF: dict[str, str] = dict(zip(DIGIT_ROW, SHIFTED_DIGIT_ROW, strict=True))


def _goto_entry(name: str) -> dict[str, Any]:
    return {"action": "page.goto", "args": {"name": name}}


def _default_v1_id_goto_bindings() -> dict[str, dict[str, Any]]:
    """The v1-ID-based digit bindings (Phase 9 Task 0) -- one `page.goto`
    entry per `marquee.PAGE_IDS` entry, keyed by the digit/shifted-digit
    character the module docstring's "Digit <-> v1-ID mapping formula"
    section derives from that page's own v1 ID (verified against v1's
    real keymap, docs/visual-audit.md, including that section's one
    disclosed deviation for ID 10/"tuner").

    Every `PAGE_IDS` entry gets a binding UNCONDITIONALLY, regardless of
    whether its page is in any particular build's roster -- `page.goto`'s
    own graceful no-op (`engine/core.py::Engine._page_goto`, this task's
    own addition) is what makes a v1-mapped-but-roster-absent page (any
    `PAGE_IDS` entry excluded by a narrowed/custom `config.pages` roster
    -- the STOCK default roster, as of Phase 9 Task 3, 96b1c12 "feat:
    live tuner", currently includes every `PAGE_IDS`-mapped page, "tuner"
    incl., so this scenario is only reachable via a hand-edited roster
    today, not out of the box) harmless rather than a dispatch error, the
    same "ordinary, expected situation" category `page.jump`'s own
    out-of-range no-op already established -- just reached through
    `page.goto`'s own handler instead of a new one.

    Raises `ValueError` at IMPORT time (never at some later dispatch) if a
    future `PAGE_IDS` edit ever introduces a v1 ID >= 20 (this formula's
    two-bank 0-9/10-19 assumption) or two pages sharing one ID -- both
    would otherwise silently collide two page names onto the same key, a
    far worse failure mode than refusing to import."""
    bindings: dict[str, dict[str, Any]] = {}
    seen_ids: set[int] = set()
    for name, page_id in _V1_PAGE_IDS.items():
        if page_id in seen_ids:
            raise ValueError(
                f"marquee.PAGE_IDS: duplicate v1 ID {page_id} (page {name!r}) -- the "
                "digit-nav formula (engine/keymap.py) requires unique IDs")
        seen_ids.add(page_id)
        tens, ones = divmod(page_id, 10)
        if tens not in (0, 1):
            raise ValueError(
                f"marquee.PAGE_IDS: v1 ID {page_id} (page {name!r}) is outside the 0-19 "
                "range the digit-nav formula (engine/keymap.py) supports -- add a third bank")
        digit_char = str(ones)
        key = digit_char if tens == 0 else _SHIFT_OF[digit_char]
        bindings[key] = _goto_entry(name)
    return bindings


# See module docstring's "Default-keymap reality check" -- the original
# three real hardcoded keys (unchanged), plus Phase 8 Task 6's `?` help-
# overlay toggle, plus Phase 9 Task 0's v1-ID-based digit/shifted-digit
# `page.goto` bindings (superseding Phase 8 Task 6's own roster-positional
# `page.jump` bindings for THIS table specifically -- see module
# docstring's "Digit navigation: v1 page-ID based" section).
DEFAULT_KEYMAP: dict[str, Any] = {
    "q": CLIENT_QUIT_ACTION,
    "c": "eventlog.clear",
    "n": "page.next",
    "?": CLIENT_HELP_TOGGLE_ACTION,
    **_default_v1_id_goto_bindings(),
}

# Per-page default bindings (Phase 8 Task 6): v1's per-page retuning keys,
# restored via this schema's args-table mechanism where the underlying
# engine action requires args -- see docs/visual-audit.md's per-page key
# tables and task-6-report.md for the audit-row -> action -> key mapping
# this was built from, including what's DELIBERATELY not (yet) covered
# here (most of spectrum's 23 v1 retuning keys, img2txtviz gamma/block-
# size/fps-cap/auto-quality -- all disclosed gaps, not silent ones, see
# each page module's own docstring). Pianoroll pitch-window panning WAS
# in that gap list -- Phase 10 Task A (docs/demo-feedback-2026-08-12.md
# item 11) closes it below via "KEY_UP"/"KEY_DOWN" -- see
# PianorollState.pan_by's own docstring for the v1 file:line semantics.
DEFAULT_PAGE_KEYMAPS: dict[str, dict[str, Any]] = {
    "pianoroll": {
        "[": {"action": "pianoroll.zoom", "args": {"delta": -0.1}},
        "]": {"action": "pianoroll.zoom", "args": {"delta": 0.1}},
        "p": "pianoroll.projection_toggle",
        "d": {"action": "pianoroll.channel_toggle", "args": {"channel": 10}},
        "*": {"action": "pianoroll.channels", "args": {"spec": ""}},
        # "KEY_UP"/"KEY_DOWN": the canonical arrow-key names this whole
        # codebase uses for the FIRST time (Phase 10 Task A) -- matches
        # blessed's own `Keystroke.name` convention (`clients/tui.py`'s
        # `run_tui` normalizes a sequence keypress to `key.name` before
        # this lookup) AND v1's own naming for this exact feature
        # (`~/codex/midicrt/pages/pianoroll.py:208,213`, `ch.name ==
        # "KEY_UP"`/`"KEY_DOWN"`) -- `clients/fb/app.py`'s evdev table and
        # the web client's keydown handler both normalize their own raw
        # input to these SAME strings, see each one's own comment.
        "KEY_UP": {"action": "pianoroll.pan", "args": {"delta": 1}},
        "KEY_DOWN": {"action": "pianoroll.pan", "args": {"delta": -1}},
    },
    "img2txtviz": {
        "i": "img2txtviz.invert",
        "c": "img2txtviz.charset",
    },
    "sendnotes": {
        # v1's QWERTY-row white/black note keys (pages/sendnotes.py's own
        # module docstring has the full mapping this ports) -- every one
        # of these was PRESENT-as-text/UNBOUND per the audit (the engine's
        # single `sendnotes.key {key: str}` catch-all action already
        # existed; only the client-side keymap binding was missing).
        "z": {"action": "sendnotes.key", "args": {"key": "z"}},
        "s": {"action": "sendnotes.key", "args": {"key": "s"}},
        "x": {"action": "sendnotes.key", "args": {"key": "x"}},
        "d": {"action": "sendnotes.key", "args": {"key": "d"}},
        "c": {"action": "sendnotes.key", "args": {"key": "c"}},
        "v": {"action": "sendnotes.key", "args": {"key": "v"}},
        "g": {"action": "sendnotes.key", "args": {"key": "g"}},
        "b": {"action": "sendnotes.key", "args": {"key": "b"}},
        "h": {"action": "sendnotes.key", "args": {"key": "h"}},
        "j": {"action": "sendnotes.key", "args": {"key": "j"}},
        "m": {"action": "sendnotes.key", "args": {"key": "m"}},
        "l": {"action": "sendnotes.key", "args": {"key": "l"}},
        "n": {"action": "sendnotes.key", "args": {"key": "n"}},
        ";": {"action": "sendnotes.key", "args": {"key": ";"}},
        "/": {"action": "sendnotes.key", "args": {"key": "/"}},
        # Control keys (pages/sendnotes.py::apply_key's own docstring --
        # these branch BEFORE the note-KEYMAP lookup, so "," and "."
        # below can never actually trigger their own KEYMAP-mapped notes,
        # a real, faithfully-preserved v1 quirk, not a bug this binding
        # introduces): "," / "." channel -/+, "[" / "]" octave -/+,
        # "-" / "=" velocity -/+, "g" / "h" gate -/+ (already bound above
        # as note triggers -- SAME real shadowing: the engine-side handler
        # itself always resolves "g"/"h" as gate adjustment, never notes).
        ",": {"action": "sendnotes.key", "args": {"key": ","}},
        ".": {"action": "sendnotes.key", "args": {"key": "."}},
        "[": {"action": "sendnotes.key", "args": {"key": "["}},
        "]": {"action": "sendnotes.key", "args": {"key": "]"}},
        "-": {"action": "sendnotes.key", "args": {"key": "-"}},
        "=": {"action": "sendnotes.key", "args": {"key": "="}},
    },
    "spectrum": {
        # Representative, minimal subset of v1's 23 retuning keys --
        # `analyzers/spectrum.py::SpectrumAnalyzer` gained real mutable
        # `bins` state for this task (`SpectrumAnalyzer.adjust_bins`); the
        # other 21 (gain/smoothing/floor/ceiling/display-scale/auto-adapt/
        # lin-log/agg-mode/low-cut/HPF-toggle/device-cycle) need their own
        # analyzer-side state additions and are deliberately deferred, see
        # docs/visual-audit.md's own build-priority notes and this task's
        # report for the full disclosure.
        "[": {"action": "spectrum.bins", "args": {"delta": -8}},
        "]": {"action": "spectrum.bins", "args": {"delta": 8}},
    },
}


def _looks_like_page_section(value: Any) -> bool:
    """A `[keys]` entry is a nested per-page section (not an args-table
    entry for a literal key named e.g. "pianoroll") iff it's a dict with NO
    `"action"` key -- see module docstring's "Schema v2" section."""
    return isinstance(value, dict) and "action" not in value


def _validate_entry_shape(value: Any) -> tuple[bool, Any]:
    """Normalize/validate ONE `[keys]`-or-`[keys.<page>]` entry value.
    Returns `(True, normalized_entry)` for a valid string or args-table
    shape, `(False, None)` for anything else (caller logs + skips)."""
    if isinstance(value, str):
        return True, value
    if isinstance(value, dict) and "action" in value:
        action = value.get("action")
        args = value.get("args", {})
        if isinstance(action, str) and isinstance(args, dict):
            return True, {"action": action, "args": dict(args)}
        return False, None
    return False, None


def _read_raw_keys_table(path: str) -> dict[str, Any]:
    """Read `path`'s top-level `keys` value (raw, unvalidated-per-entry) --
    `{}` if the file doesn't exist or has no `[keys]` table at all. Raises
    `ValueError` if `keys` exists but isn't itself a table (module
    docstring's "Malformed-file resilience" section -- same structural
    guard the original Phase 4 `load_keymap` already had)."""
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    keys = raw.get("keys", {})
    if not isinstance(keys, dict):
        raise ValueError(  # noqa: TRY004 -- see original module's own precedent
            f"keymap.toml's top-level 'keys' must be a table of key=action "
            f"pairs, got {type(keys).__name__}: {keys!r}")
    return keys


def _split_global_and_pages(
        raw_keys: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Partition a raw `[keys]` table into (global flat entries, {page:
    {entries}}) -- see module docstring's "Schema v2" section for the
    `"action"`-key discriminator. Per-entry shape errors are logged and
    skipped individually (one bad line never fails the whole file), same
    "log + skip this one entry, keep going" precedent the original
    Phase 4 `load_keymap` already established."""
    global_entries: dict[str, Any] = {}
    page_entries: dict[str, dict[str, Any]] = {}
    for key, value in raw_keys.items():
        if _looks_like_page_section(value):
            section: dict[str, Any] = {}
            for pkey, pvalue in value.items():
                ok, normalized = _validate_entry_shape(pvalue)
                if not ok:
                    _LOG.warning(
                        "keymap: skipping [keys.%s] entry %r = %r -- must be a string "
                        "action name or a {action=..., args={...}} table", key, pkey, pvalue)
                    continue
                section[pkey] = normalized
            page_entries[key] = section
            continue
        if not isinstance(key, str):
            _LOG.warning("keymap: skipping [keys] entry %r = %r -- key must be a string",
                        key, value)
            continue
        ok, normalized = _validate_entry_shape(value)
        if not ok:
            _LOG.warning(
                "keymap: skipping [keys] entry %r = %r -- must be a string action name or a "
                "{action=..., args={...}} table", key, value)
            continue
        global_entries[key] = normalized
    return global_entries, page_entries


def load_keymap(path: str | None = None) -> dict[str, Any]:
    """Read `path`'s GLOBAL `[keys]` entries (flat, non-page-section ones)
    and merge them on top of `DEFAULT_KEYMAP` -- unchanged Phase 4 contract
    (same function name/signature/merge semantics every existing caller
    already relies on), now additionally accepting args-table-shaped
    values alongside plain strings. Returns raw (normalized-shape, not yet
    registry-validated) entries -- see `filter_known_actions` for that
    separate step."""
    raw_keys = _read_raw_keys_table(path or DEFAULT_PATH)
    global_entries, _pages = _split_global_and_pages(raw_keys)
    merged = dict(DEFAULT_KEYMAP)
    merged.update(global_entries)
    return merged


def load_page_keymaps(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Read `path`'s `[keys.<page>]` sections and merge each one on top of
    `DEFAULT_PAGE_KEYMAPS.get(page, {})`, key by key -- the per-page
    counterpart to `load_keymap`'s global merge (module docstring's "Merge
    semantics" section). A page named in the file that has no default
    section of its own still gets one (starting from `{}`); a page in
    `DEFAULT_PAGE_KEYMAPS` not mentioned in the file keeps its defaults
    untouched."""
    raw_keys = _read_raw_keys_table(path or DEFAULT_PATH)
    _global, page_overrides = _split_global_and_pages(raw_keys)
    merged = {name: dict(entries) for name, entries in DEFAULT_PAGE_KEYMAPS.items()}
    for page, overrides in page_overrides.items():
        merged[page] = {**merged.get(page, {}), **overrides}
    return merged


def _validate_args_table_against_schema(action: str, args: dict, schema: dict) -> bool:
    """An args-table entry must supply EXACTLY the action's schema arg
    names -- FULLY specified, no more, no fewer (module docstring's
    "Schema v2" section: "the args are FULLY specified in the keymap
    entry"). `filter_known_actions` cannot see `ActionRegistry.register`'s
    per-arg `defaults` (deliberately not exposed over `describe()`'s wire
    shape, see engine/actions.py's own docstring), so a schema arg with a
    server-side default still must be named here -- the alternative (allow
    omitting it) would mean this function can't tell "the caller forgot an
    arg" from "this arg happens to have a default in THIS build" without a
    registry it doesn't have access to. A real type mismatch (e.g. a
    string where the schema wants an int) is NOT caught here -- that's
    `ActionRegistry.dispatch`'s own `_COERCERS` job at actual dispatch
    time, backstopped by both clients' existing non-fatal `ClientError`
    handling for a rejected action (`clients/tui.py::_handle_key_press`,
    `clients/fb/app.py::_dispatch_evdev_key`) -- deliberately not
    duplicated here to keep this module decoupled from `engine/actions.py`'s
    coercion internals."""
    return set(args) == set(schema)


def filter_known_actions(keymap: dict[str, Any], actions: dict[str, dict]) -> dict[str, Any]:
    """Drop any entry whose action is neither a `client.*` pseudo-action
    NOR present in `actions` (`ActionRegistry.describe()`'s own shape) --
    logged, not raised, matching docs/phase4-notes.md's "fail gracefully
    ... never at load" contract for roster-dependent action names.

    Two entry shapes, two rules (module docstring's "Schema v2" section):
      - a plain STRING action name that requires args (`actions[action]
        ["args"]` non-empty) is dropped -- unchanged Phase 4 behavior,
        `dispatch_key` sends a bare string action with no args at all, so
        an args-requiring action bound as a plain string can never
        actually be satisfied.
      - an ARGS-TABLE entry (`{"action": ..., "args": {...}}`) is kept iff
        its baked `args` fully match the action's schema (`_validate_
        args_table_against_schema` above) -- this is what makes an
        args-requiring action bindable at all, PROVIDED the keymap author
        supplied every arg the schema needs.
    Before this check existed (Phase 4), binding an args-requiring action
    anyway reached the engine's `ActionRegistry.dispatch` (`engine/
    actions.py`), which raises `ActionError("missing arg: ...")` --
    surfaced to a client as `ClientError`. Both clients now treat a
    rejected dispatch as non-fatal (see their own key-handling docstrings),
    but closing the door here at validation time remains the primary,
    load-bearing fix for a STRING-shaped entry naming an args-requiring
    action; args-table entries close the SAME door in the other direction
    (accept it, once verified complete)."""
    filtered: dict[str, Any] = {}
    for key, entry in keymap.items():
        if isinstance(entry, str):
            if entry.startswith("client."):
                filtered[key] = entry
                continue
            info = actions.get(entry)
            if info is None:
                _LOG.warning("keymap: key %r maps to unknown action %r, skipping", key, entry)
                continue
            if info.get("args"):
                _LOG.warning(
                    "keymap: key %r maps to action %r, which requires args %s and is not "
                    "bindable via a single keypress with no args table -- skipping",
                    key, entry, sorted(info["args"]))
                continue
            filtered[key] = entry
        elif isinstance(entry, dict):
            action = entry.get("action")
            args = entry.get("args", {})
            if not isinstance(action, str):
                _LOG.warning("keymap: key %r has a malformed args-table entry %r, skipping",
                            key, entry)
                continue
            if action.startswith("client."):
                # An args-table entry naming a client-local pseudo-action
                # makes no sense (pseudo-actions never take args) -- treat
                # it as malformed rather than silently discarding the args.
                _LOG.warning(
                    "keymap: key %r binds pseudo-action %r via an args table, which is "
                    "never valid -- skipping", key, action)
                continue
            info = actions.get(action)
            if info is None:
                _LOG.warning("keymap: key %r maps to unknown action %r, skipping", key, action)
                continue
            schema = info.get("args", {})
            if not _validate_args_table_against_schema(action, args, schema):
                _LOG.warning(
                    "keymap: key %r binds action %r with args %s, which doesn't fully match "
                    "its schema %s -- skipping", key, action, sorted(args), sorted(schema))
                continue
            filtered[key] = {"action": action, "args": dict(args)}
        else:
            _LOG.warning("keymap: key %r has a malformed entry %r, skipping", key, entry)
    return filtered


def load_keymap_or_warn(path: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Safe wrapper around `load_keymap` -- deliberately NEVER raises. See
    module docstring's "Malformed-file resilience" section. Returns
    `(keymap, None)` on success, or `(None, warning_message)` on failure."""
    try:
        return load_keymap(path), None
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return None, f"keymap.toml failed to load ({path or DEFAULT_PATH}): {exc}"


def load_page_keymaps_or_warn(
        path: str | None = None) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Safe wrapper around `load_page_keymaps`, same never-raises contract
    as `load_keymap_or_warn` above."""
    try:
        return load_page_keymaps(path), None
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return None, f"keymap.toml failed to load ({path or DEFAULT_PATH}): {exc}"
