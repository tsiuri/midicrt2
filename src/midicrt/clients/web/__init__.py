"""midicrt-web: aiohttp browser dashboard for the midicrt engine daemon.

Phase-6 (ahead-of-schedule track): observer parity first (read-only,
`--allow-control` off by default), a control surface second. See
`bridge.py` for the EngineClient<->asyncio fan-out design and `app.py` for
the HTTP/WS wiring.
"""
