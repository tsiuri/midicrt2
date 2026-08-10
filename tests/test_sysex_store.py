"""TDD for `engine/sysex_store.py::SysexStore` in isolation (no `Engine`
needed) -- the ring of recently-received frames, name sanitization/path-
traversal defense, the on-disk library's atomic save/list/play/delete-to-
trash, and the loopprogress-style status-text decay. Engine-level wiring
(the `overlay.sysex` analyzer wrapper, `sysex.list/save/play/delete`
actions, ENOSPC->ActionError containment, replay suppression, provenance)
is covered in test_engine_sysex_store.py instead -- mirrors test_capture.py's
own "pure logic here, registry-aware engine wiring there" split (see that
file's own module comment).

Distinct from test_engine_sysex.py/test_engine_sysex_dispatch.py, which
cover the PRE-EXISTING, unrelated `engine/sysex.py` Cirklon remote-control
COMMAND protocol (F0 7D 6D 63 <cmd> ... F7) -- this module is the NEW
general-purpose SysEx MANAGER (Phase 9 Task 5, user-requested: "record,
save, and play sysex at will from the browser"), which records EVERY
incoming sysex frame regardless of whether it's addressed to midicrt at
all.
"""
import json

import pytest

from midicrt.engine import sysex_store as sysex_store_mod
from midicrt.engine.sysex_store import (
    MAX_FRAME_BYTES,
    RING_SIZE,
    SYSEX_DISPLAY_SECS,
    SysexStore,
    resolve_sysex_dir,
    sanitize_name,
)


def make_event(ts=1000.0, source="Cirklon", data=(0x7D, 0x01, 0x02)):
    return type("FakeEvent", (), {"ts": ts, "source": source, "sysex_data": data})()


class _FakeMidiOut:
    def __init__(self, ok=True):
        self.sent: list[tuple[int, ...]] = []
        self._ok = ok

    def send_sysex(self, data):
        self.sent.append(data)
        return self._ok


# -- resolve_sysex_dir -----------------------------------------------------

def test_resolve_sysex_dir_explicit_override_wins(tmp_path):
    override = str(tmp_path / "explicit")
    assert resolve_sysex_dir(override) == override


def test_resolve_sysex_dir_falls_back_when_default_parent_unwritable(monkeypatch, tmp_path):
    unwritable_parent = tmp_path / "no-such-parent" / "sysex"
    fallback = tmp_path / "fallback" / "sysex"
    monkeypatch.setattr(sysex_store_mod, "DEFAULT_SYSEX_DIR", str(unwritable_parent))
    monkeypatch.setattr(sysex_store_mod, "DEV_FALLBACK_SYSEX_DIR", str(fallback))
    assert resolve_sysex_dir(None) == str(fallback)


def test_resolve_sysex_dir_uses_default_when_parent_exists_and_writable(monkeypatch, tmp_path):
    default = tmp_path / "sysex"   # tmp_path itself is the writable parent
    fallback = tmp_path / "fallback" / "sysex"
    monkeypatch.setattr(sysex_store_mod, "DEFAULT_SYSEX_DIR", str(default))
    monkeypatch.setattr(sysex_store_mod, "DEV_FALLBACK_SYSEX_DIR", str(fallback))
    assert resolve_sysex_dir(None) == str(default)


# -- sanitize_name: path traversal must be structurally impossible --------

@pytest.mark.parametrize("name", [
    "my-patch", "My Patch 01", "patch_1", "a", "A" * 63,
])
def test_sanitize_name_accepts_reasonable_names(name):
    assert sanitize_name(name) == name.strip()


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "..", "../x", "a/b", "a\\b", "/etc/passwd",
    "", "   ", ".", "..hidden", "a" * 200, None, "a/../../b",
])
def test_sanitize_name_rejects_traversal_and_garbage(name):
    with pytest.raises(ValueError):
        sanitize_name(name)


# -- ring: record_received -------------------------------------------------

def test_record_received_appends_to_recent_newest_first(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(ts=1.0, data=(0x7D, 0x01)))
    store.record_received(make_event(ts=2.0, data=(0x41, 0x02, 0x03)))
    recent = store.recent()
    assert [r["ts"] for r in recent] == [2.0, 1.0]
    assert recent[0]["size"] == 3
    assert recent[1]["size"] == 2


def test_ring_is_bounded_to_ring_size(tmp_path):
    store = SysexStore(library_dir=str(tmp_path), ring_size=3)
    for i in range(5):
        store.record_received(make_event(ts=float(i), data=(0x00,)))
    recent = store.recent()
    assert len(recent) == 3
    assert [r["ts"] for r in recent] == [4.0, 3.0, 2.0]


@pytest.mark.parametrize("data,expected", [
    ((0x7D, 0x01), "Non-Commercial"),
    ((0x41, 0x01), "Roland"),
    ((0x43, 0x01), "Yamaha"),
    ((0x99,), "0x99"),
    ((0x00, 0x01, 0x02), "ext:0102"),
    ((), "?"),
])
def test_manufacturer_decode_best_effort(tmp_path, data, expected):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(data=data))
    assert store.recent()[0]["manufacturer"] == expected


def test_oversized_frame_is_not_kept_in_memory_but_still_visible(tmp_path):
    store = SysexStore(library_dir=str(tmp_path), max_frame_bytes=4)
    store.record_received(make_event(data=(1, 2, 3, 4, 5, 6)))
    recent = store.recent()
    assert recent[0]["size"] == 6
    assert recent[0]["truncated"] is True
    with pytest.raises(ValueError):
        store.save("too-big", index=0)


def test_status_text_and_active_decay(tmp_path):
    store = SysexStore(library_dir=str(tmp_path), display_secs=5.0)
    store.record_received(make_event(ts=100.0, data=(0x7D,)))
    assert store.status_text
    assert store.status_active(100.0) is True
    assert store.status_active(104.9) is True
    assert store.status_active(105.1) is False


def test_status_active_false_before_any_activity(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    assert store.status_active(0.0) is False


# -- library: save ---------------------------------------------------------

def test_save_writes_an_atomic_json_file_under_library_dir(tmp_path):
    lib = tmp_path / "sysex"
    store = SysexStore(library_dir=str(lib))
    store.record_received(make_event(ts=1.0, source="Cirklon", data=(0x7D, 0x01, 0x02)))
    result = store.save("my patch", now=42.0)
    assert result["name"] == "my patch"
    assert result["size"] == 3
    path = lib / "my patch.json"
    assert path.exists()
    doc = json.loads(path.read_text())
    assert doc["data"] == [0x7D, 0x01, 0x02]
    assert doc["saved_ts"] == 42.0
    # no stray tempfile left behind
    assert [p.name for p in lib.iterdir()] == ["my patch.json"]


def test_save_default_index_zero_saves_most_recent_frame(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(ts=1.0, data=(0x01,)))
    store.record_received(make_event(ts=2.0, data=(0x02, 0x03)))
    result = store.save("latest")
    assert result["size"] == 2


def test_save_by_explicit_index_picks_the_right_recent_frame(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(ts=1.0, data=(0x01,)))       # index 1 after next
    store.record_received(make_event(ts=2.0, data=(0x02, 0x03)))  # index 0
    result = store.save("older", index=1)
    assert result["size"] == 1


def test_save_unknown_index_raises_value_error(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event())
    with pytest.raises(ValueError):
        store.save("x", index=5)


def test_save_rejects_traversal_name_and_writes_nothing_outside_dir(tmp_path):
    lib = tmp_path / "sysex"
    store = SysexStore(library_dir=str(lib))
    store.record_received(make_event())
    with pytest.raises(ValueError):
        store.save("../../etc/passwd")
    # nothing was created at all -- neither inside nor (obviously) outside
    assert not (tmp_path / "etc").exists()
    assert not lib.exists() or list(lib.iterdir()) == []


def test_save_updates_status_text(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event())
    store.save("x", now=50.0)
    assert "x" in store.status_text
    assert store.status_active(50.0) is True


# -- library: list_library ---------------------------------------------------

def test_list_library_empty_when_dir_absent(tmp_path):
    store = SysexStore(library_dir=str(tmp_path / "nope"))
    assert store.list_library() == []


def test_list_library_lists_saved_entries(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(data=(0x41, 0x01)))
    store.save("alpha")
    store.record_received(make_event(data=(0x42, 0x01)))
    store.save("beta")
    names = sorted(row["name"] for row in store.list_library())
    assert names == ["alpha", "beta"]


def test_list_library_skips_malformed_entries_without_raising(tmp_path, caplog):
    lib = tmp_path / "sysex"
    lib.mkdir()
    (lib / "corrupt.json").write_text("{not valid json")
    (lib / "not-a-sysex-file.txt").write_text("ignore me")
    store = SysexStore(library_dir=str(lib))
    store.record_received(make_event(data=(0x41,)))
    store.save("good")
    names = [row["name"] for row in store.list_library()]
    assert names == ["good"]


def test_list_library_ignores_the_trash_subdirectory(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(data=(0x41,)))
    store.save("gone")
    store.delete("gone")
    assert store.list_library() == []


# -- library: play -----------------------------------------------------------

def test_play_sends_via_midi_out_and_reports_size(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(data=(0x7D, 0x01, 0x02)))
    store.save("x")
    out = _FakeMidiOut()
    result = store.play("x", out, now=10.0)
    assert result == {"sent": True, "size": 3}
    assert out.sent == [(0x7D, 0x01, 0x02)]
    assert store.status_active(10.0) is True


def test_play_unknown_name_raises_value_error(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    with pytest.raises(ValueError):
        store.play("nope", _FakeMidiOut())


def test_play_rejects_traversal_name(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    with pytest.raises(ValueError):
        store.play("../../etc/passwd", _FakeMidiOut())


def test_play_corrupt_library_file_raises_value_error_not_crash(tmp_path):
    lib = tmp_path / "sysex"
    lib.mkdir()
    (lib / "bad.json").write_text("{not json")
    store = SysexStore(library_dir=str(lib))
    with pytest.raises(ValueError):
        store.play("bad", _FakeMidiOut())


def test_play_reports_sent_false_when_output_unavailable(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    store.record_received(make_event(data=(0x01,)))
    store.save("x")
    result = store.play("x", _FakeMidiOut(ok=False))
    assert result["sent"] is False


# -- library: delete (stage-don't-delete) -------------------------------------

def test_delete_moves_file_to_trash_subdir_not_removing_it(tmp_path):
    lib = tmp_path / "sysex"
    store = SysexStore(library_dir=str(lib))
    store.record_received(make_event(data=(0x7D, 0x01)))
    store.save("x")
    src = lib / "x.json"
    assert src.exists()
    result = store.delete("x")
    assert result == {"deleted": True}
    assert not src.exists()
    trash = lib / "trash" / "x.json"
    assert trash.exists()
    assert json.loads(trash.read_text())["data"] == [0x7D, 0x01]


def test_delete_unknown_name_raises_value_error(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    with pytest.raises(ValueError):
        store.delete("nope")


def test_delete_rejects_traversal_name(tmp_path):
    store = SysexStore(library_dir=str(tmp_path))
    with pytest.raises(ValueError):
        store.delete("../../etc/passwd")


def test_delete_twice_does_not_raise_second_time_overwrites_trash(tmp_path):
    lib = tmp_path / "sysex"
    store = SysexStore(library_dir=str(lib))
    store.record_received(make_event(data=(0x01,)))
    store.save("x")
    store.delete("x")
    store.record_received(make_event(data=(0x02, 0x03)))
    store.save("x")
    store.delete("x")   # must not raise even though trash/x.json already existed
    assert json.loads((lib / "trash" / "x.json").read_text())["data"] == [0x02, 0x03]


# -- constants sanity (documented, not arbitrary) -----------------------------

def test_ring_size_and_max_frame_bytes_are_sane_documented_defaults():
    assert RING_SIZE >= 1
    assert MAX_FRAME_BYTES >= 1024
    assert SYSEX_DISPLAY_SECS > 0
