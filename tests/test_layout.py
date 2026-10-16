"""The package layout of SPEC.md, and the stdlib-only rule for the core."""

import ast
import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "fetchguard"

MODULES = [
    "fetchguard",
    "fetchguard.errors",
    "fetchguard.clock",
    "fetchguard.policy",
    "fetchguard.gate",
    "fetchguard.checks",
    "fetchguard.traffic",
    "fetchguard.status",
    "fetchguard.store",
    "fetchguard.store.memory",
    "fetchguard.store.sqlite",
    "fetchguard.store.postgres",
    "fetchguard.adapters",
    "fetchguard.adapters.urllib",
    "fetchguard.adapters.httpx",
    "fetchguard.adapters.requests",
    "fetchguard.testing",
    "fetchguard.testing.barrier",
    "fetchguard.testing.simulator",
    "fetchguard.testing.counter",
]

# Third-party imports allowed per file; everything else must be stdlib.
EXTRAS = {
    "adapters/httpx.py": {"httpx"},
    "adapters/requests.py": {"requests"},
    "store/postgres.py": set(),  # the driver is chosen in T7
    "testing/plugin.py": {"pytest"},
}


@pytest.mark.parametrize("name", MODULES)
def test_module_exists_and_says_what_it_is_for(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__
    assert module.__doc__.strip()


def test_package_is_typed() -> None:
    assert (SRC / "py.typed").is_file()


def imported_top_levels(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")),
    ids=lambda p: str(p.relative_to(SRC)),
)
def test_core_uses_only_the_standard_library(path: Path) -> None:
    allowed = EXTRAS.get(path.relative_to(SRC).as_posix(), set())
    foreign = {
        name
        for name in imported_top_levels(path)
        if name not in sys.stdlib_module_names and name != "fetchguard"
    }
    assert foreign <= allowed, f"{path.name} imports {sorted(foreign - allowed)}"
