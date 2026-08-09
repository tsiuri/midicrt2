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
keys (design decision, brief's own framing)
-------------------------------------------------------------------------
The ruling asks for numeric keys to jump by ROSTER POSITION ("1st page",
"5th page"), not by a fixed page NAME -- a deploy's `config.pages` order
can differ (or change), so "5" must always mean "whatever page is 5th in
THIS build's roster" rather than a name baked in at some other build's
roster shape. Two designs were on the table:
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
current roster's length is an ordinary, expected situation (`DEFAULT_
KEYMAP` always defines all 20 positions -- 1-10 unshifted, 11-20 shifted --
regardless of how many pages THIS build actually has) and is a silent,
logged no-op instead (`engine/core.py::Engine._page_jump`'s own docstring).

Roster-position key layout (both banks are DEFAULT bindings, always
present -- a user's keymap.toml can still override/remove any of them like
any other entry)
-------------------------------------------------------------------------
  "1".."9", "0"        -> positions 1-10   (roster's first ten pages)
  "!@#$%^&*()"          -> positions 11-20  (roster's next ten pages)
(the shifted-row layout mirrors a standard US keyboard's shift-1..shift-0
row: "!" is shift+1, ")" is shift+0, etc.)

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
# in the SAME left-to-right order as "1234567890". Used both to build the
# second (11-20) bank of default jump bindings below AND by fb's evdev
# shifted-char table (`clients/fb/app.py::_build_evdev_shifted_char_table`)
# so the two stay derived from one source of truth, not two hand-typed
# copies of the same layout.
DIGIT_ROW = "1234567890"
SHIFTED_DIGIT_ROW = "!@#$%^&*()"


def _jump_entry(position: int) -> dict[str, Any]:
    return {"action": PAGE_JUMP_ACTION, "args": {"position": position}}


def _default_roster_jump_bindings() -> dict[str, dict[str, Any]]:
    """The 20 default `page.jump` bindings: unshifted digit row -> positions
    1-10, shifted digit row -> positions 11-20 -- see module docstring's
    "Roster-position key layout" section."""
    bindings: dict[str, dict[str, Any]] = {}
    for i, ch in enumerate(DIGIT_ROW):
        bindings[ch] = _jump_entry(i + 1)
    for i, ch in enumerate(SHIFTED_DIGIT_ROW):
        bindings[ch] = _jump_entry(i + 11)
    return bindings


# See module docstring's "Default-keymap reality check" -- the original
# three real hardcoded keys (unchanged), plus Phase 8 Task 6's additions:
# `?` opens/closes the help overlay, and the 20 roster-positional jump
# bindings.
DEFAULT_KEYMAP: dict[str, Any] = {
    "q": CLIENT_QUIT_ACTION,
    "c": "eventlog.clear",
    "n": "page.next",
    "?": CLIENT_HELP_TOGGLE_ACTION,
    **_default_roster_jump_bindings(),
}

# Per-page default bindings (Phase 8 Task 6): v1's per-page retuning keys,
# restored via this schema's args-table mechanism where the underlying
# engine action requires args -- see docs/visual-audit.md's per-page key
# tables and task-6-report.md for the audit-row -> action -> key mapping
# this was built from, including what's DELIBERATELY not (yet) covered
# here (pianoroll pitch-window panning, most of spectrum's 23 v1 retuning
# keys, img2txtviz gamma/block-size/fps-cap/auto-quality -- all disclosed
# gaps, not silent ones, see each page module's own docstring).
DEFAULT_PAGE_KEYMAPS: dict[str, dict[str, Any]] = {
    "pianoroll": {
        "[": {"action": "pianoroll.zoom", "args": {"delta": -0.1}},
        "]": {"action": "pianoroll.zoom", "args": {"delta": 0.1}},
        "p": "pianoroll.projection_toggle",
        "d": {"action": "pianoroll.channel_toggle", "args": {"channel": 10}},
        "*": {"action": "pianoroll.channels", "args": {"spec": ""}},
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
