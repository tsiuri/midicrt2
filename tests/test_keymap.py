"""TDD for engine/keymap.py: the config-served keymap loader (Phase 4 Task
1, docs/phase4-notes.md). Two independent halves tested here:

  - `load_keymap`: pure TOML parsing + merge-with-defaults, no awareness of
    the engine's action registry at all.
  - `filter_known_actions`: the separate, registry-aware validation step
    `Engine.__init__` calls at construction time (see engine/core.py) --
    unknown REAL actions logged + skipped, `client.*` pseudo-actions always
    passed through untouched regardless of `known_actions`.
"""
import logging

from midicrt.engine.keymap import (
    CLIENT_QUIT_ACTION,
    DEFAULT_KEYMAP,
    filter_known_actions,
    load_keymap,
)


def test_default_keymap_matches_documented_reality():
    # Verified against both clients' ACTUAL current key handling (not the
    # task brief's own illustrative [keys] schema example, which lists a
    # "p" = "page.prev" entry as a SAMPLE, not a claim about pre-existing
    # behavior) -- see module docstring's own "Default-keymap reality
    # check". Neither client has ever bound "p" to anything.
    assert DEFAULT_KEYMAP == {
        "q": "client.quit",
        "c": "eventlog.clear",
        "n": "page.next",
    }
    assert CLIENT_QUIT_ACTION == "client.quit"
    assert "p" not in DEFAULT_KEYMAP


def test_load_keymap_missing_file_returns_defaults(tmp_path):
    result = load_keymap(str(tmp_path / "nope.toml"))
    assert result == DEFAULT_KEYMAP
    assert result is not DEFAULT_KEYMAP  # a copy, not the module's own mutable dict


def test_load_keymap_adds_a_new_key_on_top_of_defaults(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = "eventlog.clear"\n')
    result = load_keymap(str(p))
    assert result == {
        "q": "client.quit", "c": "eventlog.clear", "n": "page.next", "v": "eventlog.clear",
    }


def test_load_keymap_file_overrides_a_default_key(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nn = "page.prev"\n')
    result = load_keymap(str(p))
    # "n" is remapped; "q"/"c" keep their built-in defaults -- MERGE, not
    # whole-table replacement (a deliberate departure from config.py's own
    # whole-field-replacement convention -- see module docstring for why:
    # requiring every user keymap.toml to redeclare all bindings just to
    # remap one key would silently drop "q"=quit the moment they didn't).
    assert result["n"] == "page.prev"
    assert result["q"] == "client.quit"
    assert result["c"] == "eventlog.clear"


def test_load_keymap_missing_keys_table_returns_pure_defaults(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('# no [keys] table at all\n')
    result = load_keymap(str(p))
    assert result == DEFAULT_KEYMAP


def test_load_keymap_default_path_used_when_none_given(monkeypatch, tmp_path):
    fake_default = tmp_path / "keymap.toml"
    fake_default.write_text('[keys]\nv = "page.next"\n')
    monkeypatch.setattr("midicrt.engine.keymap.DEFAULT_PATH", str(fake_default))
    result = load_keymap(None)
    assert result["v"] == "page.next"


def test_filter_known_actions_keeps_known_real_action():
    keymap = {"n": "page.next"}
    actions = {"page.next": {"description": "", "args": {}},
              "page.prev": {"description": "", "args": {}}}
    assert filter_known_actions(keymap, actions) == {"n": "page.next"}


def test_filter_known_actions_drops_unknown_real_action_and_logs(caplog):
    keymap = {"z": "sendnotes.key"}
    actions = {"page.next": {"description": "", "args": {}}}
    with caplog.at_level(logging.WARNING):
        result = filter_known_actions(keymap, actions)
    assert result == {}
    assert "sendnotes.key" in caplog.text
    assert "z" in caplog.text


def test_filter_known_actions_passes_through_client_pseudo_action_even_if_unknown():
    keymap = {"q": "client.quit", "x": "client.made_up"}
    result = filter_known_actions(keymap, {})   # empty registry -- nothing is "known"
    assert result == {"q": "client.quit", "x": "client.made_up"}


def test_filter_known_actions_mixed_keeps_known_and_client_drops_unknown():
    keymap = {"q": "client.quit", "n": "page.next", "z": "bogus.action"}
    actions = {"page.next": {"description": "", "args": {}}}
    result = filter_known_actions(keymap, actions)
    assert result == {"q": "client.quit", "n": "page.next"}


# -- args-requiring actions are not bindable via a single keypress ----------
# (Critical, bindings review -- live-reproduced: six shipping actions
# require args -- sendnotes.key, page.goto, pianoroll.zoom/.projection/
# .channels, pagecycle.enable -- and `dispatch_key` (clients/base.py) has no
# way to supply an argument VALUE for a keypress. Binding one anyway used to
# crash the TUI (ActionError -> ClientError -> the client's own
# connection-loss exit path) or silently no-op forever on fb. Caught here,
# at the SAME "log + skip, never raise" point as an unknown action name.

def test_filter_known_actions_drops_action_that_requires_args_and_logs(caplog):
    keymap = {"v": "page.goto"}
    actions = {"page.goto": {"description": "Jump to a named page", "args": {"name": "str"}}}
    with caplog.at_level(logging.WARNING):
        result = filter_known_actions(keymap, actions)
    assert result == {}
    assert "page.goto" in caplog.text
    assert "args" in caplog.text.lower()


def test_filter_known_actions_keeps_a_real_action_with_no_args():
    keymap = {"n": "page.next"}
    actions = {"page.next": {"description": "", "args": {}}}
    assert filter_known_actions(keymap, actions) == {"n": "page.next"}


def test_filter_known_actions_client_pseudo_action_bypasses_args_check():
    # client.* names have no registry entry/args schema at all -- must
    # never be rejected on an "args" basis, only unknown REAL actions are.
    keymap = {"q": "client.quit"}
    assert filter_known_actions(keymap, {}) == {"q": "client.quit"}


def test_filter_known_actions_drops_every_shipping_args_requiring_action():
    # The exact six the review named -- verified together so a future
    # action's args schema shrinking to {} (making it newly bindable)
    # doesn't silently make this test meaningless for the others.
    actions = {
        "sendnotes.key": {"description": "", "args": {"key": "str"}},
        "page.goto": {"description": "", "args": {"name": "str"}},
        "pianoroll.zoom": {"description": "", "args": {"delta": "float"}},
        "pianoroll.projection": {"description": "", "args": {"mode": "str"}},
        "pianoroll.channels": {"description": "", "args": {"spec": "str"}},
        "pagecycle.enable": {"description": "", "args": {"enabled": "bool"}},
    }
    keymap = {k: name for k, name in zip("uiopjk", actions, strict=True)}
    assert filter_known_actions(keymap, actions) == {}


# -- malformed keymap.toml must never propagate an exception -----------------
# (Critical, bindings review -- live-reproduced: an unguarded `load_keymap`
# crashed daemon startup on a malformed file, and tore down the requesting
# connection on a `config.reload`. `load_keymap_or_warn` is the ONE shared
# safe wrapper both call sites in engine/core.py use.)

def test_load_keymap_or_warn_success_returns_keymap_and_no_warning(tmp_path):
    from midicrt.engine.keymap import load_keymap_or_warn

    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = "eventlog.clear"\n')
    keymap, warning = load_keymap_or_warn(str(p))
    assert warning is None
    assert keymap["v"] == "eventlog.clear"


def test_load_keymap_or_warn_missing_file_returns_defaults_and_no_warning(tmp_path):
    from midicrt.engine.keymap import load_keymap_or_warn

    keymap, warning = load_keymap_or_warn(str(tmp_path / "nope.toml"))
    assert warning is None
    assert keymap == DEFAULT_KEYMAP


def test_load_keymap_or_warn_malformed_toml_returns_none_and_a_warning(tmp_path):
    from midicrt.engine.keymap import load_keymap_or_warn

    p = tmp_path / "keymap.toml"
    p.write_text("this is not valid toml {{{ [[[ ===\n")
    keymap, warning = load_keymap_or_warn(str(p))
    assert keymap is None
    assert warning is not None
    assert "keymap.toml" in warning.lower()


def test_load_keymap_or_warn_never_raises_on_malformed_toml(tmp_path):
    from midicrt.engine.keymap import load_keymap_or_warn

    p = tmp_path / "keymap.toml"
    p.write_text("[keys\nn = \n")   # unterminated table header -- real TOMLDecodeError
    # Must not raise -- this is the entire point of the wrapper.
    load_keymap_or_warn(str(p))
