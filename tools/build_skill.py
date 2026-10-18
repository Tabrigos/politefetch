"""Generate the API reference of the bundled agent skill from the code.

Writes ``src/politefetch/skill/politefetch/references/api.md``: every public
module, and in each the names of its ``__all__`` with their signatures and the
first paragraph of their docstrings, read from the installed package. The
hand-written ``SKILL.md`` next to it points here for details.

Usage::

    uv run python tools/build_skill.py           # rewrite the file
    uv run python tools/build_skill.py --check   # exit 1 if it is stale

``tests/test_skill.py`` fails when the committed file differs from what this
script produces, or when a public module is missing from ``MODULES``, so a
change to the public API cannot ship without it.
"""

import argparse
import ast
import enum
import importlib
import inspect
import re
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import politefetch

ROOT = Path(__file__).resolve().parent.parent
TARGET = (
    ROOT / "src" / "politefetch" / "skill" / "politefetch" / "references" / "api.md"
)

MODULES = [
    "politefetch",
    "politefetch.gate",
    "politefetch.policy",
    "politefetch.status",
    "politefetch.errors",
    "politefetch.checks",
    "politefetch.clock",
    "politefetch.adapters",
    "politefetch.adapters.base",
    "politefetch.adapters.urllib",
    "politefetch.adapters.httpx",
    "politefetch.adapters.requests",
    "politefetch.store",
    "politefetch.store.base",
    "politefetch.store.memory",
    "politefetch.store.sqlite",
    "politefetch.store.postgres",
    "politefetch.testing",
    "politefetch.testing.plugin",
    "politefetch.testing.barrier",
    "politefetch.testing.simulator",
    "politefetch.testing.counter",
    "politefetch.testing.processes",
    "politefetch.skill",
]
"""Every public module, in reading order."""

EXTRA = {
    "politefetch.adapters.httpx": "extra `httpx`",
    "politefetch.adapters.requests": "extra `requests`",
    "politefetch.store.postgres": "extra `postgres`",
    "politefetch.testing.plugin": "pytest plugin, installed with the package",
}

_QUALIFIED = re.compile(r"\b(?:[a-z_]\w*\.)+([A-Za-z_]\w*)")
_FUNCTION = re.compile(r"<function ([\w.]+) at 0x[0-9a-f]+>")
_ROLE = re.compile(r":(?:class|meth|func|data|mod|attr):`~?(?:[\w.]*\.)?([^`]+)`")


def _short(text: str) -> str:
    """Drop module prefixes: ``datetime.timedelta`` -> ``timedelta``."""
    text = text.replace("<class '", "").replace("'>", "")
    text = _FUNCTION.sub(r"\1", text)
    return _QUALIFIED.sub(r"\1", text)


def _paragraph(obj: Any) -> str:
    """The first paragraph of a docstring, on one line."""
    doc = inspect.getdoc(obj) or ""
    first = doc.split("\n\n", 1)[0]
    return _ROLE.sub(r"`\1`", " ".join(line.strip() for line in first.splitlines()))


def _signature(obj: Any) -> str:
    """The signature without `self` and, for classes, without `-> None`."""
    try:
        text = _short(str(inspect.signature(obj)))
    except (TypeError, ValueError):
        return ""
    text = re.sub(r"^\((?:self|cls)(, )?", "(", text)
    return text.removesuffix(" -> None") if inspect.isclass(obj) else text


def _defined_here(value: Any, module: ModuleType) -> bool:
    return getattr(value, "__module__", module.__name__) == module.__name__


def _attribute_docs(cls: type) -> dict[str, str]:
    """The string literals that follow class attributes in the source."""
    body = ast.parse(textwrap.dedent(inspect.getsource(cls))).body[0]
    assert isinstance(body, ast.ClassDef)
    docs = {}
    for node, after in zip(body.body, body.body[1:], strict=False):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and isinstance(after, ast.Expr)
            and isinstance(after.value, ast.Constant)
            and isinstance(after.value.value, str)
        ):
            text = after.value.value.strip().split("\n\n", 1)[0]
            docs[node.targets[0].id] = " ".join(text.split())
    return docs


def _members(cls: type) -> list[str]:
    out = []
    for name, member in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            out.append(f"- `.{name}` (property): {_paragraph(member)}".rstrip(": "))
        elif isinstance(member, staticmethod | classmethod):
            func = member.__func__
            out.append(f"- `.{name}{_signature(func)}`: {_paragraph(func)}")
        elif inspect.isfunction(member):
            line = f"- `.{name}{_signature(member)}`"
            doc = _paragraph(member)
            out.append(f"{line}: {doc}" if doc else line)
    return out


def _class(name: str, cls: type) -> list[str]:
    out = [f"### `{name}`", ""]
    bases = [b.__name__ for b in cls.__bases__ if b not in (object, enum.StrEnum)]
    if issubclass(cls, enum.Enum):
        out += [_paragraph(cls), ""]
        docs = _attribute_docs(cls)
        for m in cls:
            line = f"- `{m.name}` = `{m.value!r}`"
            out.append(f"{line}: {docs[m.name]}" if m.name in docs else line)
        out.append("")
        return out
    if getattr(cls, "_is_protocol", False):
        out += [f"Protocol. {_paragraph(cls)}", ""]
    else:
        heading = f"`{name}{_signature(cls)}`"
        if bases and bases != ["Protocol"]:
            heading += f" (subclass of {', '.join(f'`{b}`' for b in bases)})"
        out += [heading, "", _paragraph(cls), ""]
    members = _members(cls)
    if members:
        out += [*members, ""]
    return out


def _entry(module: ModuleType, name: str) -> list[str]:
    value = getattr(module, name)
    if inspect.isclass(value):
        return _class(name, value)
    if inspect.isfunction(value):
        return [f"### `{name}{_signature(value)}`", "", _paragraph(value), ""]
    if name == "__version__":
        return [f"### `{name}`", "", "The package version, as a string.", ""]
    return [f"### `{name}`", "", f"`{_short(repr(value))}`", ""]


def _fixtures(module: ModuleType) -> list[str]:
    out = []
    for name, value in sorted(vars(module).items()):
        if name.startswith("_") or not hasattr(value, "_fixture_function_marker"):
            continue
        out += [f"### fixture `{name}`", "", _paragraph(value), ""]
    return out


def _module(module_name: str) -> list[str]:
    module = importlib.import_module(module_name)
    title = f"## `{module_name}`"
    if module_name in EXTRA:
        title += f" ({EXTRA[module_name]})"
    out = [title, "", _paragraph(module), ""]
    names = list(getattr(module, "__all__", []))
    here = [n for n in names if _defined_here(getattr(module, n), module)]
    elsewhere = [n for n in names if n not in here]
    if elsewhere:
        listed = ", ".join(
            f"`{n}` ({getattr(module, n).__module__})"
            if hasattr(getattr(module, n), "__module__")
            else f"`{n}`"
            for n in elsewhere
        )
        out += [f"Re-exports: {listed}.", ""]
    for name in here:
        out += _entry(module, name)
    out += _fixtures(module)
    return out


def build() -> str:
    """Return the content of ``references/api.md``."""
    out = [
        f"# politefetch {politefetch.__version__}: API reference",
        "",
        "<!-- Generated by tools/build_skill.py from the code. Do not edit by hand:",
        "     run `uv run python tools/build_skill.py` after changing the API. -->",
        "",
        "Read only the section you need. Times are aware UTC `datetime` objects;",
        "durations accept `timedelta` or strings such as `'90s'`, `'15m'`, `'2h'`;",
        "sizes accept `int` bytes or strings such as `'50MB'`, `'1GiB'`.",
        "",
        "Modules: " + " · ".join(f"`{m}`" for m in MODULES),
        "",
    ]
    for module_name in MODULES:
        out += _module(module_name)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if stale")
    args = parser.parse_args(argv)
    content = build()
    if args.check:
        current = TARGET.read_text("utf-8") if TARGET.exists() else ""
        if current != content:
            print(f"{TARGET} is stale: run `uv run python tools/build_skill.py`")
            return 1
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
