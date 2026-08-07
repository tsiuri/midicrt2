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
-- OR one requiring args a single keypress can never supply, see that
function's own docstring -- is logged and dropped at ENGINE CONSTRUCTION
time, never at TOML-parse time -- exactly the "action vocabulary is
roster-dependent ... fail gracefully ... never at load" contract
docs/phase4-notes.md's architecture-facts section calls for.

Malformed-file resilience (bindings review, live-reproduced Critical
finding)
-------------------------------------------------------------------------
`load_keymap` itself still RAISES on a syntactically invalid TOML file
(`tomllib.TOMLDecodeError`, a `ValueError` subclass) -- it stays a small,
pure, raising function, matching `config.py::load()`'s own precedent of
letting a parse error propagate. `load_keymap_or_warn` is the separate
safe wrapper `engine/core.py`'s two production call sites (`__init__` and
`_config_reload`) actually use -- a bad optional config file must never
crash daemon startup nor tear down a `config.reload` requester's
connection; see that function's own docstring for the full incident.
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
    `filter_known_actions`'s separate job.

    Structure validation (re-review, live-reproduced Critical finding):
    a syntactically VALID TOML file can still have the WRONG SHAPE at
    "keys" -- `keys = "oops"`, `keys = 5`, `keys = ["a"]` are all legal
    TOML, but none is a table, so a naive `.items()` call on it raises an
    uncaught `AttributeError` (NOT a `ValueError`/`TOMLDecodeError`,
    invisible to `load_keymap_or_warn`'s catch tuple). "The daemon won't
    start after I edited keymap.toml" is one bug class regardless of
    whether the edit was a syntax typo or a shape typo -- so this function
    validates explicitly instead of ever reaching that attribute access:
      - `raw["keys"]` (if present) must itself be a table (a Python
        `dict`, once parsed) -- anything else raises `ValueError` with a
        readable message, letting `load_keymap_or_warn`'s EXISTING catch
        tuple handle it exactly like a syntax error (boot -> defaults +
        warning; reload -> last-good + warning, connection alive).
      - Within a valid table, an individual entry whose key or action
        ISN'T a string (e.g. `n = 5`, a bare int value) is skipped with
        its own logged warning -- same "log + skip this one entry, keep
        going" precedent `filter_known_actions` already sets for an
        unknown action name, rather than failing the WHOLE file over one
        bad line."""
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return dict(DEFAULT_KEYMAP)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    overrides = raw.get("keys", {})
    if not isinstance(overrides, dict):
        # Deliberately ValueError, not TypeError (ruff TRY004 would prefer
        # the latter for a plain type-mismatch) -- `load_keymap_or_warn`'s
        # catch tuple is `(OSError, ValueError, tomllib.TOMLDecodeError)`,
        # matching `config.py`'s own "bad config data is a ValueError"
        # convention (`ConfigError` is itself a `ValueError` subclass); a
        # `TypeError` here would NOT be caught there, reopening exactly
        # the uncaught-exception hole this fix exists to close.
        raise ValueError(  # noqa: TRY004
            f"keymap.toml's top-level 'keys' must be a table of key=action "
            f"pairs, got {type(overrides).__name__}: {overrides!r}")
    merged = dict(DEFAULT_KEYMAP)
    for key, action in overrides.items():
        if not isinstance(key, str) or not isinstance(action, str):
            _LOG.warning(
                "keymap: skipping [keys] entry %r = %r -- both the key and the "
                "action must be strings", key, action)
            continue
        merged[key] = action
    return merged


def filter_known_actions(keymap: dict[str, str], actions: dict[str, dict]) -> dict[str, str]:
    """Drop any entry whose action is neither a `client.*` pseudo-action
    NOR present in `actions` (`ActionRegistry.describe()`'s own shape --
    `{name: {"description": ..., "args": {...}}}` -- at the moment this is
    called, which is what makes it roster-aware) -- logged, not raised,
    matching docs/phase4-notes.md's "fail gracefully ... never at load"
    contract for roster-dependent action names.

    ALSO drops (bindings review, live-reproduced Critical finding) any
    entry whose action requires args (`actions[action]["args"]` non-empty)
    -- `dispatch_key` (`clients/base.py`) sends an action with NO argument
    values at all, since it has no mechanism to supply one from a single
    keypress. Six shipping actions need this guard today: `sendnotes.key`,
    `page.goto`, `pianoroll.zoom`/`.projection`/`.channels`, and
    `pagecycle.enable`. Before this check, binding one of these anyway
    reached the engine's `ActionRegistry.dispatch` (`engine/actions.py`),
    which raises `ActionError("missing arg: ...")` -- surfaced to a client
    as `ClientError` from `EngineClient.action()`. The TUI's OLD handling
    of that treated ANY `ClientError` from a key-dispatch as "connection
    lost" and exited the whole client; fb's OLD handling silently
    swallowed it forever with no diagnostic at all. Both are real, but
    THIS function closing the door at validation time is the primary,
    load-bearing fix -- the client-side handling (`clients/tui.py`'s
    `_handle_key_press`, `clients/fb/app.py`'s `_dispatch_evdev_key`) is a
    defense-in-depth backstop for any binding that somehow slips through
    (e.g. a future action whose args schema changes after a keymap.toml
    was already validated against an older build)."""
    filtered = {}
    for key, action in keymap.items():
        if action.startswith("client."):
            filtered[key] = action
            continue
        info = actions.get(action)
        if info is None:
            _LOG.warning("keymap: key %r maps to unknown action %r, skipping", key, action)
            continue
        if info.get("args"):
            _LOG.warning(
                "keymap: key %r maps to action %r, which requires args %s and is not "
                "bindable via a single keypress -- skipping",
                key, action, sorted(info["args"]))
            continue
        filtered[key] = action
    return filtered


def load_keymap_or_warn(path: str | None = None) -> tuple[dict[str, str] | None, str | None]:
    """Safe wrapper around `load_keymap` (bindings review, live-reproduced
    Critical finding): a malformed/unreadable `keymap.toml` -- most
    realistically a TOML syntax error from hand-editing the file --
    otherwise raised `tomllib.TOMLDecodeError` (a `ValueError` subclass)
    straight out of `load_keymap`, past BOTH of `engine/core.py`'s call
    sites. At `Engine.__init__` time that crashed daemon STARTUP entirely
    on a bad optional config file -- exactly the appliance-ethos violation
    `config.py`'s own `ConfigError` docstring already calls out for a
    different failure mode. At `config.reload` time it tore down the
    REQUESTING CONNECTION (an uncaught exception escaping an action
    handler propagates past `ActionRegistry.dispatch`'s own `except
    ActionError` narrowing, which does not catch this).

    Returns `(keymap, None)` on success, or `(None, warning_message)` on
    failure -- deliberately NEVER raises. Callers decide what "keep the
    old value" means for their own call site: `Engine.__init__` has
    nothing to fall back to yet but `DEFAULT_KEYMAP`; `_config_reload` has
    the previous `self.keymap` to leave untouched."""
    try:
        return load_keymap(path), None
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return None, f"keymap.toml failed to load ({path or DEFAULT_PATH}): {exc}"
