"""Pure, no-socket unit tests for clients/cli.py -- the argparse/wire-glue
pieces small enough to test directly without a real daemon (contrast
test_daemon_cli.py, which is exclusively subprocess-against-a-real-daemon).
"""
from midicrt.clients.cli import _format_learn_bound, _parse_args


def test_parse_args_splits_kv_pairs():
    assert _parse_args(["a=1", "b=two"]) == {"a": "1", "b": "two"}


def test_parse_args_empty_list_is_empty_dict():
    assert _parse_args([]) == {}


def test_parse_args_value_may_contain_equals_signs():
    assert _parse_args(["expr=a=b"]) == {"expr": "a=b"}


# -- _format_learn_bound (review fix: replace-on-relearn CLI reporting) -----

def test_format_learn_bound_with_no_replacement_only_shows_the_new_binding():
    data = {"binding": {"id": "learn_1", "action": "page.next"}, "replaced": []}
    text = _format_learn_bound(data)
    assert "learn_1" in text
    assert "replaced" not in text.lower()


def test_format_learn_bound_reports_a_replaced_binding():
    data = {"binding": {"id": "learn_2", "action": "page.prev"},
            "replaced": [{"id": "learn_1", "action": "page.next"}]}
    text = _format_learn_bound(data)
    assert "replaced" in text.lower()
    assert "learn_1" in text
    assert "page.next" in text
    assert "learn_2" in text   # the new binding is still reported too


def test_format_learn_bound_reports_multiple_replaced_bindings():
    data = {"binding": {"id": "learn_3", "action": "page.prev"},
            "replaced": [{"id": "learn_1", "action": "page.next"},
                        {"id": "learn_2", "action": "page.goto"}]}
    text = _format_learn_bound(data)
    assert "learn_1" in text
    assert "learn_2" in text
