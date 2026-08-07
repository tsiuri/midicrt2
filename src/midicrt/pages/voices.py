"""Voices page: v1's main screen (see docs/evidence-phase2-smoke/after.png)
-- 16 instrument rows, one per MIDI channel, showing live + peak polyphony
and the currently-held note numbers. Wraps `analyzers.voices.
VoiceMonitorAnalyzer` (the pure state machine -- see that module for the
full v1 behavioral notes: note-on/off pairing, velocity-0-as-off, no
sustain, CC120/123 + start/stop clears) and adds the one thing that's
page-specific, not analyzer-specific: binding each channel row to its
configured instrument NAME (`config.instruments`, task-4 brief).
"""
from midicrt.analyzers.voices import VoiceMonitorAnalyzer


class VoicesPage:
    name = "voices"

    def __init__(self, instruments: list[str]):
        self._analyzer = VoiceMonitorAnalyzer()
        self._names = list(instruments)

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
        }
