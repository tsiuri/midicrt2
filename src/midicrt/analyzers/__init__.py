"""Analyzers: pure state machines fed `MidiEvent`s, one per overlay topic.

An analyzer has the same shape as a `Page` (spec/engine/core.py's `Page`
Protocol) -- `handle(ev) -> bool` (dirty convention: True means "my
view_model() changed"), `view_model() -> dict` -- but publishes under an
`overlay.<name>` topic instead of `page.<name>`, so a client can subscribe
to it ALONGSIDE whatever page is current (see clients/chrome.py). Analyzers
never read a clock or do I/O: all timing comes from the `ts` (and, for
`clock_tick`, `clock_batch_start`) fields already on the `MidiEvent` --
see analyzers/transport.py for the first one and the rationale.
"""
