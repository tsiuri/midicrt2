"""Engine-side behaviors: idle-timer-driven automation that acts ONLY
through the action API (spec's "behaviors act only through actions" rule --
see docs/phase3-notes.md's contracts section, `engine/actions.py`'s
`ActionRegistry`, and this package's own modules' docstrings).

A behavior is a pure, synchronous state machine, structurally unable to
touch engine/page state directly: `tick(now, last_activity_ts,
current_page) -> (action_name, args) | None` reads three plain values
handed to it by `Engine._tick_behaviors` (mirroring `analyzers/
stucknotes.py`'s injected-`now` "engine does I/O, the object stays pure"
split) and returns, at most, ONE action intent for the ENGINE to dispatch
through `ActionRegistry.dispatch` -- a behavior never calls `dispatch`
itself, never holds a reference to the `Engine`, and never mutates a page
or analyzer in place. This is stronger than the analyzer/page convention
(which only promises "no I/O"): a behavior's only channel to affect
anything, ever, is that one optional return value.
"""
