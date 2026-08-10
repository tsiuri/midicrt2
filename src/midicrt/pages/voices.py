"""Voices page: v1's main screen (see docs/evidence-phase2-smoke/after.png)
-- 16 instrument rows, one per MIDI channel, showing live + peak polyphony
and the currently-held note numbers. Wraps `analyzers.voices.
VoiceMonitorAnalyzer` (the pure state machine -- see that module for the
full v1 behavioral notes: note-on/off pairing, velocity-0-as-off, no
sustain, CC120/123 + start/stop clears, and Phase 9 Task 2's poly-limit
event log) and adds the two things that are page-specific, not
analyzer-specific: binding each channel row to its configured instrument
NAME (`config.instruments`, task-4 brief), and forwarding the analyzer's
`events` log verbatim into this page's own view-model (task brief: "rolling
event log surfaced in the voices page view-model").
"""
from midicrt.analyzers.voices import VoiceMonitorAnalyzer


class VoicesPage:
    name = "voices"

    def __init__(self, instruments: list[str], analyzer: VoiceMonitorAnalyzer | None = None):
        # `analyzer` is OPTIONAL, defaulting to a fresh, independent
        # instance with v1's own default limits -- mirrors
        # `pages/harmony.py::HarmonyPage`'s identical convention. `engine/
        # core.py::Engine.__init__` passes in a SHARED, config-limited
        # instance (also wrapped by `_PolyLimitOverlay` for the
        # `overlay.polylimit` chrome flash) -- see that wiring site's own
        # comment and `analyzers/voices.py`'s shared-instance dedup guard
        # for why sharing is safe, not just cheaper.
        self._analyzer = analyzer if analyzer is not None else VoiceMonitorAnalyzer()
        self._names = list(instruments)

    @property
    def analyzer(self) -> VoiceMonitorAnalyzer:
        """Exposes the underlying analyzer so Engine.__init__ can hand the
        SAME instance to the `overlay.polylimit` chrome-flash wrapper --
        see __init__'s own comment."""
        return self._analyzer

    def set_instruments(self, instruments: list[str]) -> None:
        """Phase 4 Task 1 (`config.reload`, docs/phase4-notes.md): live-swap
        the per-channel instrument names read from config.toml's
        `instruments` list, without reconstructing the page -- which would
        also discard the live `VoiceMonitorAnalyzer`'s currently-tracked
        note/poly state mid-run. Cheap and safe: `self._names` is plain
        data with no analyzer coupling at all (see `view_model`'s own use
        of it, purely as a label lookup by channel index)."""
        self._names = list(instruments)

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def view_model(self) -> dict:
        vm = self._analyzer.view_model()
        rows = []
        for i, ch_vm in enumerate(vm["channels"]):
            ch = i + 1
            name = self._names[i] if i < len(self._names) else f"CH {ch}"
            rows.append({
                "ch": ch,
                "name": name,
                "active": ch_vm["active"],
                "peak": ch_vm["peak"],
                "notes": ch_vm["notes"],
            })
        return {
            "title": "VOICES",
            "rows": rows,
            "total": vm["total"],
            "total_peak": vm["total_peak"],
            # Phase 9 Task 2: forwarded verbatim, same shape as
            # `VoiceMonitorAnalyzer.view_model()`'s own "events" field.
            "events": vm["events"],
        }
