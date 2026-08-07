"""TDD for HelpPage (page name "help") -- a read-only reference built from
the engine's own describe data (roster + action registry), NOT a literal
port of v1's static keybinding list -- see pages/help.py's own module
docstring for why this is the v2-appropriate-equivalent parity port.
"""
from midicrt.pages.help import _IDLE_HELP_INFO, HelpPage


def test_view_model_shape_and_title():
    page = HelpPage()
    vm = page.view_model()
    assert vm["title"] == "HELP"
    assert "page_rows" in vm
    assert "action_rows" in vm


def test_falls_back_to_idle_info_without_a_bound_engine():
    page = HelpPage()
    vm = page.view_model()
    assert vm["page_rows"] == [{"label": "pages (page.goto <name>)", "value": "(none)"}]
    assert vm["action_rows"] == []
    assert _IDLE_HELP_INFO == {"pages": [], "actions": {}}


def test_bound_info_reflects_the_live_roster_and_actions():
    page = HelpPage()
    page.bind_info(lambda: {
        "pages": ["eventlog", "voices"],
        "actions": {
            "page.next": {"description": "Advance to the next page", "args": {}},
            "page.goto": {"description": "Jump to a named page", "args": {"name": "str"}},
        },
    })
    vm = page.view_model()
    assert vm["page_rows"] == [
        {"label": "pages (page.goto <name>)", "value": "eventlog, voices"}]
    assert vm["action_rows"] == [
        {"label": "page.goto", "value": "Jump to a named page  (name:str)"},
        {"label": "page.next", "value": "Advance to the next page"},
    ]


def test_action_rows_are_sorted_by_name_regardless_of_input_order():
    page = HelpPage()
    page.bind_info(lambda: {
        "pages": [],
        "actions": {
            "z.last": {"description": "z", "args": {}},
            "a.first": {"description": "a", "args": {}},
        },
    })
    names = [row["label"] for row in page.view_model()["action_rows"]]
    assert names == ["a.first", "z.last"]


def test_handle_never_marks_dirty():
    page = HelpPage()
    assert page.handle(object()) is False
