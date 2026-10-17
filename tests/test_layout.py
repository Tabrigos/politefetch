"""The package layout of SPEC.md, and the stdlib-only rule for the core."""

import ast
import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "politefetch"

MODULES = [
    "politefetch",
    "politefetch.errors",
    "politefetch.clock",
    "politefetch.policy",
    "politefetch.gate",
    "politefetch.checks",
    "politefetch.traffic",
    "politefetch.status",
    "politefetch.store",
    "politefetch.store.base",
    "politefetch.store.memory",
    "politefetch.store.sqlite",
    "politefetch.store.postgres",
    "politefetch.adapters",
    "politefetch.adapters.base",
    "politefetch.adapters.urllib",
    "politefetch.adapters.httpx",
    "politefetch.adapters.requests",
    "politefetch.testing",
    "politefetch.testing.barrier",
    "politefetch.testing.plugin",
    "politefetch.testing.processes",
    "politefetch.testing.simulator",
    "politefetch.testing.counter",
]

# Third-party imports allowed per file; everything else must be stdlib.
EXTRAS = {
    "adapters/httpx.py": {"httpx"},
    "adapters/requests.py": {"requests", "urllib3"},
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
        if name not in sys.stdlib_module_names and name != "politefetch"
    }
    assert foreign <= allowed, f"{path.name} imports {sorted(foreign - allowed)}"


TESTS = Path(__file__).resolve().parent

# Ways to start a process that bypass politefetch.testing.processes.spawn.
FORBIDDEN_MODULES = {"subprocess", "multiprocessing"}
FORBIDDEN_CALLS = {
    ("os", "fork"),
    ("os", "forkpty"),
    ("os", "system"),
    ("os", "popen"),
    ("os", "posix_spawn"),
    ("os", "posix_spawnp"),
    *{
        ("os", f"exec{suffix}")
        for suffix in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")
    },
    *{
        ("os", f"spawn{suffix}")
        for suffix in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")
    },
    ("concurrent.futures", "ProcessPoolExecutor"),
}


def process_starts(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"from {node.module} import ...")
            for alias in node.names:
                if (node.module, alias.name) in FORBIDDEN_CALLS:
                    found.append(f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            owner = ast.unparse(node.value)
            if (owner, node.attr) in FORBIDDEN_CALLS:
                found.append(f"{owner}.{node.attr}")
    return found


@pytest.mark.parametrize(
    "path", sorted(TESTS.rglob("*.py")), ids=lambda p: str(p.relative_to(TESTS))
)
def test_tests_start_processes_only_through_spawn(path: Path) -> None:
    # A child started any other way runs without the network barrier (D-012).
    assert process_starts(path) == []


def test_the_process_guard_catches_the_usual_ways(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\n"
        "import multiprocessing.pool\n"
        "from multiprocessing import Process\n"
        "from concurrent.futures import ProcessPoolExecutor\n"
        "import os\n"
        "os.fork()\n"
        "os.system('true')\n"
        "os.execv('/bin/true', [])\n",
        encoding="utf-8",
    )
    assert process_starts(sample) == [
        "import subprocess",
        "import multiprocessing.pool",
        "from multiprocessing import ...",
        "from concurrent.futures import ProcessPoolExecutor",
        "os.fork",
        "os.system",
        "os.execv",
    ]
