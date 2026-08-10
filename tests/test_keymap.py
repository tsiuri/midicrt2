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

import pytest

from midicrt.analyzers.marquee import PAGE_IDS
from midicrt.config import Config
from midicrt.engine.keymap import (
    _SHIFT_OF,
    CLIENT_HELP_TOGGLE_ACTION,
    CLIENT_QUIT_ACTION,
    DEFAULT_KEYMAP,
    DEFAULT_PAGE_KEYMAPS,
    PAGE_JUMP_ACTION,
    filter_known_actions,
    load_keymap,
    load_page_keymaps,
)


def test_default_keymap_matches_documented_reality():
    # Verified against both clients' ACTUAL current key handling -- see
    # module docstring's own "Default-keymap reality check". Phase 8
    # Task 6 (keymap revamp) added the help-overlay toggle on top of the
    # original three real keys; neither client has ever bound "p" to
    # anything at the GLOBAL level (pianoroll's own [keys.pianoroll]
    # section does bind "p", see DEFAULT_PAGE_KEYMAPS below -- that's a
    # page-scoped override, not a global default). `PAGE_JUMP_ACTION`
    # stays a valid action name even though Phase 9 Task 0 (below) no
    # longer uses it to build DEFAULT_KEYMAP's own digit bindings.
    assert DEFAULT_KEYMAP["q"] == "client.quit"
    assert DEFAULT_KEYMAP["c"] == "eventlog.clear"
    assert DEFAULT_KEYMAP["n"] == "page.next"
    assert DEFAULT_KEYMAP["?"] == "client.help_toggle"
    assert CLIENT_QUIT_ACTION == "client.quit"
    assert CLIENT_HELP_TOGGLE_ACTION == "client.help_toggle"
    assert PAGE_JUMP_ACTION == "page.jump"


# -- digit navigation: v1 page-ID based (Phase 9 Task 0, docs/gui-phase-  --
# -- decisions-2026-08-08.md's Phase-8-CLOSED reconciliation ruling)       --

def test_default_keymap_has_the_four_named_keys_plus_one_binding_per_v1_page_id():
    # One `page.goto` digit/shifted-digit binding per `marquee.PAGE_IDS`
    # entry (14 today) -- NOT a fixed 20-slot scheme like the superseded
    # roster-positional design, since there's no reason to reserve a key
    # for a v1 ID no page currently claims.
    named = {"q", "c", "n", "?"}
    assert len(DEFAULT_KEYMAP) == len(named) + len(PAGE_IDS)
    assert set(DEFAULT_KEYMAP) - named <= set("1234567890!@#$%^&*()")


def test_default_keymap_digit_bindings_regenerate_from_v1_page_ids():
    # The formula itself (engine/keymap.py module docstring's "Digit <->
    # v1-ID mapping formula" section): unshifted digit char == ID's ones
    # digit for IDs 0-9; for IDs 10-19, the SHIFTED variant of that same
    # ones-digit character.
    for name, page_id in PAGE_IDS.items():
        tens, ones = divmod(page_id, 10)
        expected_key = str(ones) if tens == 0 else _SHIFT_OF[str(ones)]
        assert DEFAULT_KEYMAP[expected_key] == {"action": "page.goto", "args": {"name": name}}


def test_default_keymap_illustrative_v1_id_examples():
    # The task brief's own worked examples, spelled out literally.
    assert DEFAULT_KEYMAP["0"] == {"action": "page.goto", "args": {"name": "help"}}
    assert DEFAULT_KEYMAP["1"] == {"action": "page.goto", "args": {"name": "harmony"}}
    assert DEFAULT_KEYMAP["8"] == {"action": "page.goto", "args": {"name": "pianoroll"}}
    # "!" is shift+1, but v1's own shifted-digit scheme (docs/visual-
    # audit.md) starts counting at ID 11, not ID 1 -- "!" -> chordkey.
    assert DEFAULT_KEYMAP["!"] == {"action": "page.goto", "args": {"name": "chordkey"}}
    # ID 10 ("tuner") is the one disclosed deviation from v1 (which used a
    # dedicated "t"/"T" letter key instead) -- this formula reaches it via
    # shift+0 (")") like every other ID, with no letter-key exception.
    assert DEFAULT_KEYMAP[")"] == {"action": "page.goto", "args": {"name": "tuner"}}


def test_default_keymap_has_no_binding_for_a_v1_id_gap():
    # No page claims v1 ID 3 (or 12/15/16/18/19 -- see PAGE_IDS) -- the
    # digit/shifted key simply has no default binding at all, the same
    # "genuinely absent" outcome as any other unmapped key, not a no-op
    # binding that happens to do nothing.
    assert "3" not in DEFAULT_KEYMAP
    for ch in "@%^*(":
        assert ch not in DEFAULT_KEYMAP


def test_default_keymap_omits_the_one_roster_page_with_no_v1_id():
    # "screensaver" is config.py's one default-roster page with no v1
    # page-ID concept at all (analyzers/marquee.py's own docstring) -- no
    # key in DEFAULT_KEYMAP names it via page.goto.
    goto_targets = {entry["args"]["name"] for entry in DEFAULT_KEYMAP.values()
                    if isinstance(entry, dict) and entry.get("action") == "page.goto"}
    assert "screensaver" not in goto_targets
    assert "screensaver" in Config().pages   # confirms it's a real roster member, not a typo


def test_default_keymap_v1_id_bindings_cover_every_default_roster_page_with_an_id():
    # Consistency test (task brief): every PAGE_IDS entry whose page IS in
    # the default roster gets a reachable page.goto digit binding.
    roster = set(Config().pages)
    goto_targets = {entry["args"]["name"] for entry in DEFAULT_KEYMAP.values()
                    if isinstance(entry, dict) and entry.get("action") == "page.goto"}
    for name in PAGE_IDS:
        if name in roster:
            assert name in goto_targets


def test_default_keymap_v1_id_bindings_also_cover_pages_outside_the_default_roster():
    # "tuner" (v1 ID 10) has a binding even though it's excluded from the
    # default roster -- "binding present" per the brief; see
    # engine/core.py::Engine._page_goto's graceful no-op for what actually
    # happens when it's pressed on a build that lacks it.
    assert "tuner" not in Config().pages
    goto_targets = {entry["args"]["name"] for entry in DEFAULT_KEYMAP.values()
                    if isinstance(entry, dict) and entry.get("action") == "page.goto"}
    assert "tuner" in goto_targets


def test_default_page_keymaps_cover_the_pages_this_task_restored_keys_for():
    assert set(DEFAULT_PAGE_KEYMAPS) == {"pianoroll", "img2txtviz", "sendnotes", "spectrum"}
    assert DEFAULT_PAGE_KEYMAPS["pianoroll"]["p"] == "pianoroll.projection_toggle"
    assert DEFAULT_PAGE_KEYMAPS["img2txtviz"]["i"] == "img2txtviz.invert"
    assert DEFAULT_PAGE_KEYMAPS["sendnotes"]["z"] == {
        "action": "sendnotes.key", "args": {"key": "z"}}


def test_load_keymap_missing_file_returns_defaults(tmp_path):
    result = load_keymap(str(tmp_path / "nope.toml"))
    assert result == DEFAULT_KEYMAP
    assert result is not DEFAULT_KEYMAP  # a copy, not the module's own mutable dict


def test_load_keymap_adds_a_new_key_on_top_of_defaults(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = "eventlog.clear"\n')
    result = load_keymap(str(p))
    assert result == {**DEFAULT_KEYMAP, "v": "eventlog.clear"}


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


# -- wrong-SHAPED (but syntactically valid) keymap.toml -----------------------
# (re-review, live-reproduced: finding 2 was only PARTIALLY fixed by widening
# the except tuple -- `keys = "oops"` / `keys = 5` / `keys = ["a"]` are all
# valid TOML that still crashed `load_keymap` with an uncaught
# `AttributeError: '...' object has no attribute 'items'` on `overrides.
# items()`, propagating straight through `load_keymap_or_warn`'s catch
# tuple untouched since AttributeError isn't ValueError/OSError/
# TOMLDecodeError. Per the re-review: "one bug class regardless of
# syntax-vs-shape typo" -- fixed by VALIDATING structure explicitly (never
# producing the AttributeError at all), not by widening the catch again.

def test_load_keymap_raises_valueerror_when_keys_is_a_string(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('keys = "oops"\n')
    with pytest.raises(ValueError, match="keys"):
        load_keymap(str(p))


def test_load_keymap_raises_valueerror_when_keys_is_a_list(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('keys = ["a"]\n')
    with pytest.raises(ValueError, match="keys"):
        load_keymap(str(p))


def test_load_keymap_raises_valueerror_when_keys_is_an_int(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text("keys = 5\n")
    with pytest.raises(ValueError, match="keys"):
        load_keymap(str(p))


def test_load_keymap_skips_a_non_string_value_entry_and_logs_but_keeps_the_rest(tmp_path, caplog):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nn = 5\nc = "eventlog.clear"\n')
    with caplog.at_level(logging.WARNING):
        result = load_keymap(str(p))
    assert result["n"] == "page.next"          # bad override skipped -- default preserved
    assert result["c"] == "eventlog.clear"     # rest of the map still loads
    assert "n" in caplog.text and "5" in caplog.text


def test_load_keymap_or_warn_keys_is_a_string_returns_none_and_a_warning(tmp_path):
    from midicrt.engine.keymap import load_keymap_or_warn

    p = tmp_path / "keymap.toml"
    p.write_text('keys = "oops"\n')
    keymap, warning = load_keymap_or_warn(str(p))
    assert keymap is None
    assert warning is not None
    assert "keys" in warning.lower()


def test_load_keymap_or_warn_never_raises_on_wrong_shaped_keys(tmp_path):
    from midicrt.engine.keymap import load_keymap_or_warn

    for bad in ('keys = "oops"\n', "keys = 5\n", 'keys = ["a"]\n'):
        p = tmp_path / "keymap.toml"
        p.write_text(bad)
        load_keymap_or_warn(str(p))   # must not raise, for any of these shapes


# -- Phase 8 Task 6: schema v2 -- args-table entries + per-page sections ----

def test_load_keymap_accepts_an_args_table_entry_at_the_global_level(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = {action = "page.jump", args = {position = 3}}\n')
    result = load_keymap(str(p))
    assert result["v"] == {"action": "page.jump", "args": {"position": 3}}


def test_load_keymap_does_not_treat_a_page_section_as_a_global_entry(tmp_path):
    # `[keys.pianoroll]` must land in load_page_keymaps, NOT show up as a
    # literal "pianoroll" key in the GLOBAL table.
    p = tmp_path / "keymap.toml"
    p.write_text('[keys]\nv = "eventlog.clear"\n\n[keys.pianoroll]\n"[" = "pianoroll.zoom"\n')
    result = load_keymap(str(p))
    assert "pianoroll" not in result
    assert result["v"] == "eventlog.clear"


def test_load_page_keymaps_missing_file_returns_pure_defaults(tmp_path):
    result = load_page_keymaps(str(tmp_path / "nope.toml"))
    assert result == DEFAULT_PAGE_KEYMAPS
    assert result is not DEFAULT_PAGE_KEYMAPS


def test_load_page_keymaps_merges_a_page_override_over_its_own_defaults(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys.pianoroll]\n"[" = {action = "pianoroll.zoom", args = {delta = -0.5}}\n')
    result = load_page_keymaps(str(p))
    # The overridden key changed; every OTHER pianoroll default survives.
    assert result["pianoroll"]["["] == {"action": "pianoroll.zoom", "args": {"delta": -0.5}}
    assert result["pianoroll"]["p"] == "pianoroll.projection_toggle"
    assert result["pianoroll"]["d"] == DEFAULT_PAGE_KEYMAPS["pianoroll"]["d"]


def test_load_page_keymaps_adds_a_brand_new_page_section_not_in_the_defaults(tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys.voices]\nx = "eventlog.clear"\n')
    result = load_page_keymaps(str(p))
    assert result["voices"] == {"x": "eventlog.clear"}
    assert result["pianoroll"] == DEFAULT_PAGE_KEYMAPS["pianoroll"]   # untouched


def test_load_page_keymaps_skips_a_malformed_entry_within_a_page_section_and_logs(caplog, tmp_path):
    p = tmp_path / "keymap.toml"
    p.write_text('[keys.pianoroll]\nx = 5\ny = "pianoroll.projection_toggle"\n')
    with caplog.at_level(logging.WARNING):
        result = load_page_keymaps(str(p))
    assert "x" not in result["pianoroll"]
    assert result["pianoroll"]["y"] == "pianoroll.projection_toggle"
    assert "pianoroll" in caplog.text and "x" in caplog.text


def test_load_page_keymaps_or_warn_malformed_toml_returns_none_and_a_warning(tmp_path):
    from midicrt.engine.keymap import load_page_keymaps_or_warn

    p = tmp_path / "keymap.toml"
    p.write_text("this is not valid toml {{{ [[[ ===\n")
    pages, warning = load_page_keymaps_or_warn(str(p))
    assert pages is None
    assert warning is not None


def test_load_page_keymaps_or_warn_success_returns_pages_and_no_warning(tmp_path):
    from midicrt.engine.keymap import load_page_keymaps_or_warn

    p = tmp_path / "keymap.toml"
    p.write_text('[keys.voices]\nx = "eventlog.clear"\n')
    pages, warning = load_page_keymaps_or_warn(str(p))
    assert warning is None
    assert pages["voices"] == {"x": "eventlog.clear"}


# -- Phase 8 Task 6: filter_known_actions validates args-table entries ------

def test_filter_known_actions_keeps_an_args_table_entry_matching_the_full_schema():
    keymap = {"1": {"action": "page.jump", "args": {"position": 1}}}
    actions = {"page.jump": {"description": "", "args": {"position": "int"}}}
    assert filter_known_actions(keymap, actions) == {
        "1": {"action": "page.jump", "args": {"position": 1}}}


def test_filter_known_actions_drops_an_args_table_entry_missing_a_required_arg():
    keymap = {"1": {"action": "page.jump", "args": {}}}
    actions = {"page.jump": {"description": "", "args": {"position": "int"}}}
    assert filter_known_actions(keymap, actions) == {}


def test_filter_known_actions_drops_an_args_table_entry_with_an_extra_unknown_arg():
    keymap = {"1": {"action": "page.jump", "args": {"position": 1, "bogus": True}}}
    actions = {"page.jump": {"description": "", "args": {"position": "int"}}}
    assert filter_known_actions(keymap, actions) == {}


def test_filter_known_actions_drops_an_args_table_entry_naming_an_unknown_action():
    keymap = {"1": {"action": "no.such.action", "args": {}}}
    assert filter_known_actions(keymap, {}) == {}


def test_filter_known_actions_drops_an_args_table_entry_naming_a_client_pseudo_action():
    # Pseudo-actions never take args -- an args-table entry naming one is
    # malformed, not a valid "pseudo-action bypasses the args check" case
    # (that bypass is only for STRING-shaped client.* entries).
    keymap = {"x": {"action": "client.help_toggle", "args": {}}}
    assert filter_known_actions(keymap, {}) == {}


def test_filter_known_actions_keeps_an_argless_action_bound_as_a_string_entry():
    keymap = {"i": "img2txtviz.invert"}
    actions = {"img2txtviz.invert": {"description": "", "args": {}}}
    assert filter_known_actions(keymap, actions) == {"i": "img2txtviz.invert"}
