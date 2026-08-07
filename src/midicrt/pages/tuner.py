"""Tuner page: v1's `pages/tuner.py` (PAGE_ID 10, "Tuner"), wrapping
`analyzers.tuner.TunerAnalyzer` -- see that module's docstring for why this
page shows v1's genuine idle state ("Listening...", no note locked) in
production until the (separate, not-yet-ported) audio-capture pipeline
exists to call `on_pitch_sample()` for real.

Not wired into `config.pages`' default roster (disclosed scope choice):
unlike `voices`/`harmony` (task 4/5, live by default because they show
real data with only a running daemon + MIDI input), this page can never
show anything but the idle state until that future audio task lands --
forcing a permanently-idle page into the default cycle would clutter the
roster for zero benefit today. It IS registered in `engine/core.py`'s
`_PAGE_FACTORIES`, so `config.toml`'s `pages = [..., "tuner"]` (or a test's
`Engine.register_page`) can reach it right now; there is no functional gap,
only a default-visibility one.
"""
from midicrt.analyzers.tuner import TunerAnalyzer


class TunerPage:
    name = "tuner"

    def __init__(self) -> None:
        self._analyzer = TunerAnalyzer()

    def handle(self, ev) -> bool:
        return self._analyzer.handle(ev)

    def view_model(self) -> dict:
        return {"title": "TUNER", **self._analyzer.view_model()}
