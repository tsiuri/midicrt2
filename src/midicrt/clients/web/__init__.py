"""midicrt-web: aiohttp browser dashboard for the midicrt engine daemon.

Phase-6 (ahead-of-schedule track) shipped observer parity first, control
surface second, read-only by default during the v1/v2 parity period.
Phase 9 Task 4 flipped that default (user ruling: "web control ON, no
auth") -- control is on unless `--read-only` is passed; see `app.py`'s
module docstring for the flag-inversion rationale. See `bridge.py` for the
EngineClient<->asyncio fan-out design (including its engine-restart
reconnect loop) and `app.py` for the HTTP/WS wiring.
"""
