"""CC Monitor page (page name "ccmonitor"): v1's `~/codex/midicrt/pages/
ccmonitor.py` (PAGE_ID 4) -- a per-channel table of the last few raw CC
messages. Wraps `analyzers.ccmonitor.CCMonitorAnalyzer` (the pure state
machine -- see that module for the full v1 behavioral notes) and exposes
only the per-channel half of its view model; the global last/peak/freshness
half is `pages/ccdashboard.py`'s own concern (v1 page 5).

Separate analyzer INSTANCE per page (disclosed, mirrors pages/chordkey.py's
own precedent)
---------------------------------------------------------------------------
The task brief describes "a `ccmonitor` analyzer... + two page layouts",
but v2 has no cross-page/analyzer state-sharing mechanism yet (every page
factory in `engine/core.py::_PAGE_FACTORIES` builds its own page from
`Config` alone, with no way to hand it ANOTHER page's already-built
analyzer instance -- the same "currently latent" gap `analyzers/harmony.py`'s
own docstring calls out). This page therefore owns its OWN
`CCMonitorAnalyzer()` instance, receiving the identical event stream every
other page does (`Engine._handle` calls every page's `handle(ev)`, not just
the current page's) -- a harmless duplication of computation, not of data:
both pages always agree on their `(channel, cc)` observations because they
observe the exact same events, just projected through the same analyzer
CLASS twice rather than one shared instance.
"""
from midicrt.analyzers.ccmonitor import CCMonitorAnalyzer


class CCMonitorPage:
    name = "ccmonitor"

    def __init__(self) -> None:
        self._analyzer = CCMonitorAnalyzer()

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def view_model(self) -> dict:
        return {
            "title": "CC MONITOR",
            "channels": self._analyzer.view_model()["per_channel"],
        }
