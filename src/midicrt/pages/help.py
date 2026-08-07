"""Help page (page name "help"): a read-only reference of what THIS build can
actually do, per the task-12 brief's "parity port" guidance for v1's Help
page.

v1 comparison (`~/codex/midicrt/pages/help.py`, PAGE_ID 0, "Help / Keys", 39
lines, READ-ONLY reference): a hand-written, static list of v1's global
page-switch keybindings ("1 - Notes view", "! - Chord+Key", ...) -- content
that is itself a second, drifted copy of the same facts `README.md`'s own
"Keybindings" section documents (grepped both on the Pi: the two lists
already disagree in places, e.g. v1's help.py never mentions page 7 Program
Changes at all).

Why this is a v2-appropriate-equivalent, not a literal port
---------------------------------------------------------------------------
At the time this page was first written (phase-3, task 12), v2 had no
keymap/key-to-action table at all -- `engine/server.py`'s `describe`
response shipped a `"keymap": {}` placeholder explicitly, and
`phase3-notes.md`'s own "Known-latent items" section called this out as
Phase 4, unbuilt ("Input layer needs a key->action TABLE"). A v2 help page
therefore had nothing keybinding-shaped to show yet -- but it DID have
something v1 never had: a live, structured, always-in-sync ACTION REGISTRY
(`engine/actions.py::ActionRegistry.describe()`, spec §4: "every engine
capability is a named action") and a live page roster
(`Engine.pages`/`config.pages`). Per the task brief's own explicit example
("a v2 help page rendering the keymap + action list from the engine's
describe data IS the parity port"), this page shows exactly that: the
current page roster (in cycle order, i.e. what `page.next`/`page.prev`
walk through) and the full sorted action list with descriptions/args --
the same data `midicrt describe` already reports over the wire, just
rendered on-screen.

UPDATE (Phase 4 Task 1, docs/phase4-notes.md): `describe`'s `"keymap"`
field is no longer a placeholder -- `engine/keymap.py` now serves a real,
live key->action table there, and both clients (`clients/tui.py`,
`clients/fb/app.py`) build their key dispatch from it. This page's OWN
rendering was deliberately left untouched by that task (its scope was the
keymap plumbing + client adoption, not this page) -- `view_model()` still
returns only `{page_rows, action_rows}`, no keymap rows. Adding a "--
Keymap --" section here (mirroring `_config_rows`/`_engine_rows`'s own
label/value-row shape) is a small, natural, still-unclaimed follow-up
whenever someone next touches this page.

Engine-info wiring
------------------
Mirrors `pages/configview.py`'s own "Engine-info wiring" pattern exactly:
the page needs a live `Engine.pages`/`Engine.actions.describe()` snapshot
it cannot derive from anything passed to its own constructor (no `Config`
dependency at all -- this page's content is 100% *engine-roster* facts, not
*config* facts, unlike `ConfigPage`). `Engine.__init__` calls `bind_info()`
on this page (once, right after building `self.pages`, same call site as
`ConfigPage.bind_engine_info`) with a bound method returning
`{"pages": [...], "actions": {...}}`. A page built directly by a test (no
engine involved) falls back to `_IDLE_HELP_INFO` below, matching
`ConfigPage`'s own `_IDLE_ENGINE_INFO` fallback contract.
"""

_IDLE_HELP_INFO = {"pages": [], "actions": {}}


def _pages_row(pages: list[str]) -> dict:
    return {"label": "pages (page.goto <name>)", "value": ", ".join(pages) or "(none)"}


def _action_rows(actions: dict) -> list[dict]:
    rows = []
    for name, info in sorted(actions.items()):
        args = ", ".join(f"{k}:{v}" for k, v in info.get("args", {}).items())
        value = info.get("description", "")
        if args:
            value = f"{value}  ({args})" if value else f"({args})"
        rows.append({"label": name, "value": value})
    return rows


class HelpPage:
    name = "help"

    def __init__(self) -> None:
        self._info_provider = None

    def bind_info(self, provider) -> None:
        """Wired once by `Engine.__init__` in production; never called by a
        page constructed directly by a test (see module docstring)."""
        self._info_provider = provider

    def handle(self, ev) -> bool:
        return False   # a read-only reference never changes from a MIDI event

    def view_model(self) -> dict:
        info = self._info_provider() if self._info_provider else _IDLE_HELP_INFO
        return {
            "title": "HELP",
            "page_rows": [_pages_row(info["pages"])],
            "action_rows": _action_rows(info["actions"]),
        }
