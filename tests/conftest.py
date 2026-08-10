"""Repo-wide test isolation (Phase 4 Task 2 review, Minor -- real
contamination risk found live): `Engine(Config())` constructed WITHOUT an
explicit `keymap_path`/`bindings_path` falls back to `keymap_mod.
DEFAULT_PATH`/`bindings_mod.DEFAULT_PATH` -- both real paths under whatever
machine actually runs the suite (`~/.config/midicrt/keymap.toml`/
`bindings.toml`). Dozens of existing tests across test_engine_core.py (many
predating this task, from Phase 4 Tasks 0/1) construct `Engine(Config())`
this way -- on a real deployed box (this Pi, where the daemon and the repo
share a home directory) that means the test suite can silently READ the
actually-deployed config, and `bind.remove`'s real `BindingsFile.save()`
call means a `bind.remove` test could in principle WRITE it too.

This was not a hypothetical: an orphaned `~/.config/midicrt/bindings.toml`
(harmless, empty, but real) was found sitting at the real default path
partway through this exact task, apparently left over from an earlier,
uncommitted attempt -- proof the risk is live on this exact machine, not
just theoretical.

Fix: an autouse, repo-wide fixture that monkeypatches BOTH modules'
`DEFAULT_PATH` to a per-test `tmp_path` subdirectory before any test body
runs. Patching the module ATTRIBUTE (not an env var or a copy captured at
import time) is what actually works here -- `Engine.__init__` reads
`keymap_mod.DEFAULT_PATH`/`bindings_mod.DEFAULT_PATH` as a live attribute
lookup at construction time (`keymap_path or keymap_mod.DEFAULT_PATH`), so
this redirects every such fallback for the duration of each test, with zero
per-test-file changes required. A test that explicitly overrides
`keymap_path`/`bindings_path` (or monkeypatches `DEFAULT_PATH` itself, e.g.
test_keymap.py's own `test_load_keymap_default_path_used_when_none_given`)
is unaffected either way -- pytest's `monkeypatch` fixture is function-
scoped and shared across all fixtures/the test body within one test node,
so a test's own explicit `monkeypatch.setattr(...)` simply layers on top of
(and is torn down together with) this fixture's own patch, in the same
LIFO stack, with no double-teardown or ordering hazard.

`config.py`'s own `DEFAULT_PATH` is deliberately NOT touched here --
`Engine.__init__` never reads `config.toml` at all (a `Config()` dataclass
is passed in directly); only `config_mod.load(path)` reads it, and every
IN-PROCESS call site takes an explicit path already.

**Correction (fix round after Phase 8 Task 2, "monochrome" review): the
daemon-startup path is NOT outside the test suite's reach** -- that claim
was wrong, and it was live, not hypothetical. `tests/test_fb_render.py::
_start_daemon` and `tests/test_daemon_cli.py::start_daemon` both spawn a
REAL `python -m midicrt.daemon` SUBPROCESS -- a genuinely separate OS
process this fixture's `monkeypatch.setattr()` calls can never reach (a
fresh interpreter re-imports `config.py` fresh, with its real, unpatched
`DEFAULT_PATH`). Confirmed live: before the fix, all 11 of those two
helpers' call sites spawned an unconfigured daemon that silently read the
REAL `~/.config/midicrt/config.toml` on this box (`capture_auto_start =
true`, a real production setting) and wrote real test-noise session files
into `/var/lib/midicrt/sessions` on every subprocess-daemon test run.
Fixed at the point of the actual gap, not here -- a process-boundary
monkeypatch is structurally impossible, so both helpers now pass their own
explicit `--config <tmp_path>/isolated-config.toml`
(`capture_auto_start = false` + `capture_dir` under that same `tmp_path`)
directly on the subprocess command line instead. See each helper's own
`_write_isolated_daemon_config` docstring.

Phase 5 Task 1 (event-sourced capture, docs/phase5-notes.md) extends the
SAME fixture to `engine/capture.py`'s own `DEFAULT_STATE_DIR`/
`DEV_FALLBACK_STATE_DIR` -- without this, a bare `Engine(Config())` in the
suite would resolve `CaptureSink`'s session directory via
`resolve_capture_dir()`'s real production/dev-fallback logic, which on a
CI runner or this exact live Pi (where the daemon and the repo share a
home directory) could point at a REAL `~/.local/state/midicrt/sessions`
directory -- the identical contamination risk the keymap/bindings fixture
above was written to close, just for a directory instead of two files.
Patching BOTH constants (not just `DEFAULT_STATE_DIR`) means a test can
never fall through to the real dev-fallback path either."""
import pytest

from midicrt.engine import bindings as bindings_mod
from midicrt.engine import capture as capture_mod
from midicrt.engine import keymap as keymap_mod
from midicrt.engine import sysex_store as sysex_store_mod


@pytest.fixture(autouse=True)
def _isolate_default_midicrt_config_paths(tmp_path, monkeypatch):
    fake_dir = tmp_path / "midicrt-config-isolated"
    monkeypatch.setattr(keymap_mod, "DEFAULT_PATH", str(fake_dir / "keymap.toml"))
    monkeypatch.setattr(bindings_mod, "DEFAULT_PATH", str(fake_dir / "bindings.toml"))
    monkeypatch.setattr(capture_mod, "DEFAULT_STATE_DIR", str(fake_dir / "sessions"))
    monkeypatch.setattr(capture_mod, "DEV_FALLBACK_STATE_DIR",
                        str(fake_dir / "sessions-dev-fallback"))
    # Phase 9 Task 5 (SysEx manager, engine/sysex_store.py): the SAME
    # contamination risk capture_mod's own two constants above exist to
    # close, one level further -- an unconfigured `SysexStore` (any bare
    # `Engine(Config())` in this suite) would otherwise resolve its
    # library directory via `resolve_sysex_dir()`'s real production/dev-
    # fallback logic, which on this exact live Pi (daemon + repo share a
    # home directory) could point at a REAL `/var/lib/midicrt/sysex` or
    # `~/.local/state/midicrt/sysex` -- do NOT write real test noise
    # there (binding constraint, task-5-brief.md).
    monkeypatch.setattr(sysex_store_mod, "DEFAULT_SYSEX_DIR", str(fake_dir / "sysex"))
    monkeypatch.setattr(sysex_store_mod, "DEV_FALLBACK_SYSEX_DIR",
                        str(fake_dir / "sysex-dev-fallback"))
