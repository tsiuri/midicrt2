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
    assert filter_known_actions(keymap, {"page.next", "page.prev"}) == {"n": "page.next"}


def test_filter_known_actions_drops_unknown_real_action_and_logs(caplog):
    keymap = {"z": "sendnotes.key"}
    with caplog.at_level(logging.WARNING):
        result = filter_known_actions(keymap, {"page.next"})
    assert result == {}
    assert "sendnotes.key" in caplog.text
    assert "z" in caplog.text


def test_filter_known_actions_passes_through_client_pseudo_action_even_if_unknown():
    keymap = {"q": "client.quit", "x": "client.made_up"}
    result = filter_known_actions(keymap, set())   # empty registry -- nothing is "known"
    assert result == {"q": "client.quit", "x": "client.made_up"}


def test_filter_known_actions_mixed_keeps_known_and_client_drops_unknown():
    keymap = {"q": "client.quit", "n": "page.next", "z": "bogus.action"}
    result = filter_known_actions(keymap, {"page.next"})
    assert result == {"q": "client.quit", "n": "page.next"}
