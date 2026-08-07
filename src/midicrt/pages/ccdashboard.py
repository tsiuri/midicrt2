"""CC Dashboard page (page name "ccdashboard"): v1's `~/codex/midicrt/pages/
ccgraph.py` (PAGE_ID 5, "CC Dashboard" -- v1's own comment calls it
"perfectly aligned") -- a single, insertion-ordered list of the 16 most
recently FIRST-SEEN `(channel, cc)` pairs, each with its latest value, a
bar proportional to that value, and a "LIVE"/"N.Ns ago" freshness label.
Wraps `analyzers.ccmonitor.CCMonitorAnalyzer` (the pure state machine --
see that module for the full v1 behavioral notes, including why this page
owns its OWN analyzer instance rather than sharing one with
`pages/ccmonitor.py`) and exposes the global `tracked` half of its view
model.

`tick(now)` is REQUIRED here (unlike `pages/ccmonitor.py`, which has no
tick at all): freshness is the entire reason this page's underlying view of
the SAME analyzer class differs from ccmonitor.py's -- see analyzers/
ccmonitor.py's own "Freshness... needs an injected wall clock" docstring
section for the full design (transition-only dirty, `age_s` always
accurate to within one tick period on any read regardless of dirty state).
"""
from midicrt.analyzers.ccmonitor import CCMonitorAnalyzer


class CCDashboardPage:
    name = "ccdashboard"

    def __init__(self) -> None:
        self._analyzer = CCMonitorAnalyzer()

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def tick(self, now: float) -> bool:
        return self._analyzer.tick(now)

    def view_model(self) -> dict:
        return {
            "title": "CC DASHBOARD",
            "entries": self._analyzer.view_model()["tracked"],
        }
