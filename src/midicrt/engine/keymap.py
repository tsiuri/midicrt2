"""Config-served keymap (Phase 4 Task 1, docs/phase4-notes.md): a single
`[keys]` TOML table mapping one-character keys to action names, replacing
each client's own hardcoded key->action table (`clients/tui.py`'s old
`_KEY_ACTIONS`, `clients/fb/app.py::_input_loop`'s old KEY_Q/KEY_C/KEY_N
branches) with one config-driven source of truth the engine serves over
`describe` (`engine/server.py`) and both clients build their dispatch from
at connect (`clients/base.py::dispatch_key`/`fetch_keymap`).

Default-keymap reality check (verified against both clients' ACTUAL
current source at task-1 start, not assumed)
-------------------------------------------------------------------------
  - TUI (`clients/tui.py`): `q` quit LOCALLY -- a dedicated
    `if key == "q": return 0` branch that ran BEFORE the old `_KEY_ACTIONS`
    dict was even consulted, so it was never a dispatched action at all.
    `_KEY_ACTIONS = {"c": "eventlog.clear", "n": "page.next"}`. No "p"
    binding existed.
  - fb (`clients/fb/app.py::_input_loop`): `KEY_Q` set `quit_event`
    locally; `KEY_C` sent `eventlog.clear`; `KEY_N` sent `page.next`. No
    `KEY_P` branch existed either.
Both clients therefore exposed the EXACT SAME three real keys. `page.prev`
(registered engine-side, unconditionally, in `engine/core.py`'s
`Engine.__init__` since phase 2) has NEVER been reachable from either
client's keyboard -- `DEFAULT_KEYMAP` below ports that reality exactly (3
entries, no "p") rather than the phase-4 task brief's own illustrative
`[keys]` schema example (which lists `"p" = "page.prev"` as a SCHEMA
sample of what a user's file might contain, not a claim about
pre-existing behavior). A user who wants `page.prev` on a key can now bind
it themselves via `keymap.toml` -- that's the actual point of this task.

Pseudo-actions
--------------
`client.*`-prefixed action names never reach the engine's `ActionRegistry`
at all -- they are handled entirely client-side. Today only
`CLIENT_QUIT_ACTION` ("client.quit") has real meaning to either client's
`dispatch_key` (`clients/base.py`); any OTHER `client.*` value is treated
as an unrecognized (but still harmless -- never sent to the engine)
client-local no-op there. `filter_known_actions` below passes ANY
`client.*` entry through untouched without checking it against the
engine's real action registry -- there is nothing there to check it
against, by design.

Merge semantics (deliberate departure from config.py's own convention)
-------------------------------------------------------------------------
`config.py::load()` replaces each Config FIELD wholesale when a
config.toml supplies it (e.g. a `pages = [...]` entry fully replaces the
default list, no element-wise merge). `load_keymap` below does NOT mirror
that for the `[keys]` table: a present file's entries are MERGED on top of
`DEFAULT_KEYMAP`, key by key -- so a keymap.toml containing only
`v = "eventlog.clear"` still leaves "q"/"c"/"n" bound to their built-in
defaults. The alternative (whole-table replacement) is a real footgun for
exactly the keybinding-config use case this task exists to serve: a user
who wants to remap ONE key would otherwise have to redeclare the entire
default table in their own file just to avoid silently losing `q` (quit)
the moment they didn't. This mirrors how most keybinding-override systems
(editor `keybindings.json` files, window-manager configs) work in
practice -- override what you name, keep everything else.

Validation timing (fail gracefully at dispatch, never at load)
-------------------------------------------------------------------------
`load_keymap` (pure TOML parsing + merge) never validates action NAMES
against the engine's real registry -- it has no registry to check
against, and the registry itself is roster-dependent
(docs/phase4-notes.md: some actions like `sendnotes.key` only exist when
their owning page is in `config.pages`). `filter_known_actions` is the
separate, second step that DOES have a registry to check against --
`Engine.__init__` (engine/core.py) calls it right after every action
(engine-owned AND page-declared) has been registered, so a stale
keymap.toml entry referencing an action absent from THIS build's roster
is logged and dropped at ENGINE CONSTRUCTION time, never at TOML-parse
time -- exactly the "action vocabulary is roster-dependent ... fail
gracefully ... never at load" contract docs/phase4-notes.md's
architecture-facts section calls for.
"""
import logging
import os
import tomllib

_LOG = logging.getLogger(__name__)

DEFAULT_PATH = os.path.expanduser("~/.config/midicrt/keymap.toml")

# Client-local pseudo-action: never dispatched to the engine (see module
# docstring's "Pseudo-actions" section). `clients/base.py::dispatch_key` is
# the one place that recognizes this literal string and reports "quit" to
# its caller instead of calling `client.action(...)`.
CLIENT_QUIT_ACTION = "client.quit"

# See module docstring's "Default-keymap reality check" -- exactly today's
# three real hardcoded keys, verified against both clients' actual source,
# not assumed from the task brief's illustrative schema example.
DEFAULT_KEYMAP: dict[str, str] = {
    "q": CLIENT_QUIT_ACTION,
    "c": "eventlog.clear",
    "n": "page.next",
}


def load_keymap(path: str | None = None) -> dict[str, str]:
    """Read `path`'s `[keys]` table (default `~/.config/midicrt/
    keymap.toml`) and MERGE it on top of `DEFAULT_KEYMAP`, or return
    `DEFAULT_KEYMAP` unchanged if the file doesn't exist at all -- same
    "read file if present else built-in default" contract as
    `config.py::load()`, but see module docstring's "Merge semantics"
    section for why a PRESENT file's `[keys]` table augments/overrides the
    defaults key-by-key rather than replacing the whole table. A file with
    no `[keys]` table at all (or an empty one) is equivalent to no
    overrides -- returns the pure defaults. Returns raw (key, action)
    pairs with NO validation against the engine's action registry -- see
    module docstring's "Validation timing" section for why that's
    `filter_known_actions`'s separate job."""
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return dict(DEFAULT_KEYMAP)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    overrides = raw.get("keys", {})
    merged = dict(DEFAULT_KEYMAP)
    merged.update({str(k): str(v) for k, v in overrides.items()})
    return merged


def filter_known_actions(keymap: dict[str, str], known_actions) -> dict[str, str]:
    """Drop any entry whose action is neither a `client.*` pseudo-action
    NOR present in `known_actions` (the engine's real, roster-dependent
    `ActionRegistry.describe()` keys at the moment this is called) --
    logged, not raised, matching docs/phase4-notes.md's "fail gracefully
    ... never at load" contract for roster-dependent action names.
    `known_actions` is checked with plain `in`, so any container (a
    `dict.keys()` view, a `set`, a list) works."""
    filtered = {}
    for key, action in keymap.items():
        if action.startswith("client.") or action in known_actions:
            filtered[key] = action
        else:
            _LOG.warning("keymap: key %r maps to unknown action %r, skipping", key, action)
    return filtered
