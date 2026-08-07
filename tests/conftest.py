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
call site either takes an explicit path or is a daemon-startup path outside
the test suite's reach."""
import pytest

from midicrt.engine import bindings as bindings_mod
from midicrt.engine import keymap as keymap_mod


@pytest.fixture(autouse=True)
def _isolate_default_midicrt_config_paths(tmp_path, monkeypatch):
    fake_dir = tmp_path / "midicrt-config-isolated"
    monkeypatch.setattr(keymap_mod, "DEFAULT_PATH", str(fake_dir / "keymap.toml"))
    monkeypatch.setattr(bindings_mod, "DEFAULT_PATH", str(fake_dir / "bindings.toml"))
