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
