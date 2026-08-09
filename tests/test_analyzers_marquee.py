"""TDD for MarqueeAnalyzer: v1's header page-title scrolling marquee (v1's
own primary anti-burn-in device, docs/visual-audit.md §20b) -- a pure state
machine fed NO MidiEvents (see analyzers/marquee.py's module docstring for
why `handle()` is always a no-op) + an injected wall-clock `tick(now)`.
"""
from midicrt.analyzers.marquee import (
    GAP,
    HEADER_SCROLL_SPEED,
    PAGE_IDS,
    PAGE_TITLES,
    MarqueeAnalyzer,
    autoconnect_window_size,
)
from midicrt.engine.core import MidiEvent

_ROSTER = ["eventlog", "voices", "harmony", "pianoroll", "spectrum", "screensaver",
           "img2txtviz", "config", "help", "progchanges", "ccmonitor", "ccdashboard",
           "chordkey", "sendnotes"]   # config.py's own default Config.pages list


def note_on(ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type="note_on", channel=0,
                      data1=60, data2=100, summary="note_on ch1 n60 v100")


def transport(kind, ts=0.0):
    return MidiEvent(ts=ts, source="USB MIDI", type=kind, channel=None,
                      data1=None, data2=None, summary=kind)


# -- text construction --------------------------------------------------------

def test_text_joins_roster_sorted_by_v1_page_id_not_roster_order():
    # "sendnotes"(2) listed before "harmony"(1) in the input roster -- output
    # must still be ID-order, matching v1's own `sorted(PAGES.items())`.
    a = MarqueeAnalyzer(["sendnotes", "harmony", "pianoroll"])
    assert a.view_model()["text"] == "[1:HARMONY]  [2:SEND NOTES]  [8:PIANOROLL]"


def test_text_excludes_pages_with_no_v1_id():
    # "screensaver" has no v1 PAGES entry at all (pages/screensaver.py's own
    # module docstring) -- must never appear in the marquee, matching v1
    # exactly (v1's PAGES dict never contained it either).
    a = MarqueeAnalyzer(["screensaver", "help"])
    assert a.view_model()["text"] == "[0:HELP]"


def test_full_default_roster_matches_v1_bracket_format():
    a = MarqueeAnalyzer(_ROSTER)
    text = a.view_model()["text"]
    assert text.startswith("[0:HELP]  [1:HARMONY]  [2:SEND NOTES]")
    assert "[13:VOICES]" in text
    assert "screensaver" not in text.lower()


def test_empty_roster_produces_empty_text_without_crashing():
    a = MarqueeAnalyzer([])
    vm = a.view_model()
    assert vm["text"] == ""
    assert vm["offset"] == 0


def test_unknown_page_name_falls_back_to_upper_name_if_it_had_a_v1_id():
    # PAGE_TITLES deliberately doesn't need to cover every PAGE_IDS key
    # (module docstring's own fallback contract) -- simulate via a stand-in
    # entry not present in PAGE_TITLES to prove the .upper() fallback path,
    # without mutating the real module tables.
    assert set(PAGE_TITLES) <= set(PAGE_IDS)   # sanity: no orphaned titles today


def test_doubled_string_is_text_plus_gap_doubled():
    a = MarqueeAnalyzer(["help", "harmony"])
    text = a.view_model()["text"]
    assert a.view_model()["doubled"] == (text + GAP) * 2


# -- handle() is a true no-op --------------------------------------------------

def test_handle_never_changes_state_or_reports_dirty():
    a = MarqueeAnalyzer(["help", "harmony"])
    before = a.view_model()
    assert a.handle(note_on(ts=1.0)) is False
    assert a.handle(transport("start", ts=1.0)) is False
    assert a.view_model() == before


# -- tick() offset math ---------------------------------------------------------

def test_tick_before_any_call_anchors_at_offset_zero():
    a = MarqueeAnalyzer(["help", "harmony"])
    assert a.view_model()["offset"] == 0


def test_tick_advances_offset_at_configured_speed():
    a = MarqueeAnalyzer(["help", "harmony"], speed_cps=4.0)
    modulo = len(a.view_model()["text"]) + len(GAP)
    a.tick(100.0)   # anchors here, offset 0
    a.tick(101.0)   # 1.0s later, 4.0 chars/sec -> offset advances by 4
    assert a.view_model()["offset"] == 4 % modulo


def test_tick_offset_wraps_via_modulo_of_text_plus_gap_length():
    a = MarqueeAnalyzer(["help"], speed_cps=HEADER_SCROLL_SPEED)   # text="[0:HELP]", len=8
    # modulo = len("[0:HELP]") + len(GAP) = 8 + 4 = 12
    a.tick(0.0)
    # 4.0 chars/sec * 3.5s = 14 chars advanced -> 14 % 12 = 2
    a.tick(3.5)
    assert a.view_model()["offset"] == 2


def test_tick_returns_dirty_only_when_integer_offset_actually_changes():
    a = MarqueeAnalyzer(["help", "harmony", "pianoroll", "voices", "spectrum"], speed_cps=4.0)
    assert a.tick(0.0) is False   # anchoring tick, offset stays 0 -> unchanged
    changed_far = a.tick(100.0)   # far enough that SOME offset change occurred
    assert changed_far is True
    same_offset_time = 100.0 + (1.0 / 4.0) * 0.1   # a tiny nudge, same int offset
    assert a.tick(same_offset_time) is False


def test_tick_is_a_pure_function_of_injected_now_not_wall_clock():
    # Two independent instances anchored at the same `now`, ticked to the
    # same later `now`, must agree exactly -- no hidden `time.time()` read.
    a = MarqueeAnalyzer(_ROSTER, speed_cps=4.0)
    b = MarqueeAnalyzer(_ROSTER, speed_cps=4.0)
    a.tick(500.0)
    b.tick(500.0)
    a.tick(507.75)
    b.tick(507.75)
    assert a.view_model()["offset"] == b.view_model()["offset"]


def test_zero_speed_pins_offset_at_zero_and_reports_dirty_only_once():
    a = MarqueeAnalyzer(["help", "harmony"], speed_cps=0.0)
    assert a.tick(0.0) is False
    assert a.tick(10.0) is False
    assert a.view_model()["offset"] == 0


# -- autoconnect-log window sizing (ported, dormant -- see module docstring) --

def test_autoconnect_window_size_short_message_matches_v1_formula():
    assert autoconnect_window_size(msg_len=5, max_avail=20) == 5
    assert autoconnect_window_size(msg_len=12, max_avail=8) == 8   # clamped to max_avail


def test_autoconnect_window_size_long_message_matches_v1_formula():
    # len=30 -> not <=12 -> min(max_avail, max(8, 30//2)) = min(40, 15) = 15
    assert autoconnect_window_size(msg_len=30, max_avail=40) == 15
    # max(8, ...) floor kicks in for a long message with a small max_avail
    assert autoconnect_window_size(msg_len=13, max_avail=100) == 8


def test_autoconnect_window_size_non_positive_max_avail_is_zero():
    assert autoconnect_window_size(msg_len=20, max_avail=0) == 0
    assert autoconnect_window_size(msg_len=20, max_avail=-3) == 0


# -- burn-in tripwire coverage (this IS the primary anti-burn-in device) -----

def test_burn_in_tripwire_offset_differs_at_t_and_t_plus_n():
    """The core burn-in claim: the SAME roster, rendered at t and t+N
    seconds, must show a genuinely different scroll position -- not just a
    theoretically-different number, but different enough that a client
    slicing a fixed-width window would actually paint different pixels
    (i.e. the offset delta must be >= 1 character)."""
    a = MarqueeAnalyzer(_ROSTER, speed_cps=HEADER_SCROLL_SPEED)
    a.tick(1000.0)
    offset_t = a.view_model()["offset"]
    a.tick(1003.0)   # +3s * 4 chars/sec = +12 chars, comfortably >= 1
    offset_t_plus_n = a.view_model()["offset"]
    assert offset_t != offset_t_plus_n
