"""Generate ``references/api.md`` of the bundled agent skill from the code.

For every public module in ``MODULES``: the names of its ``__all__`` (without
one, the public classes and functions it defines) with their
signatures and the first paragraph of their docstrings, the fields of classes
with their meaning, class members, enum values with the strings that follow
them in the source, pytest fixtures. A class or function is described once,
in the module that defines it when that module is in ``MODULES`` (even without
an ``__all__`` of its own), otherwise in the first module that exports it. The
hand-written ``SKILL.md`` next to it points here for details.

Usage::

    python tools/build_skill.py           # rewrite the file
    python tools/build_skill.py --check   # exit 1 if it is stale

``tests/test_skill.py`` fails when the committed file differs from what this
script produces, or when a public module is missing from ``MODULES``. The
output does not depend on the operating system or on memory addresses; run
the test on every Python of the CI matrix to keep it independent of the
version too.

A copy of the shared template (schema 1): only the constants above the line
change from one library to another.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import enum
import importlib
import inspect
import pkgutil
import re
import sys
import textwrap
import typing
from pathlib import Path
from types import ModuleType
from typing import Any

PACKAGE = "politefetch"
"""The import name of the package."""

SKILL_NAME = "politefetch"
"""The directory of the skill inside ``<package>/skill/``."""

MODULES: list[str] = [
    "politefetch",
    "politefetch.gate",
    "politefetch.policy",
    "politefetch.status",
    "politefetch.errors",
    "politefetch.checks",
    "politefetch.clock",
    "politefetch.adapters",
    "politefetch.adapters.base",
    "politefetch.adapters.decoding",
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
]
"""Every public module, in reading order. ``<package>.skill`` stays out."""

EXTRA: dict[str, str] = {
    "politefetch.adapters.httpx": "extra `httpx`",
    "politefetch.adapters.requests": "extra `requests`",
    "politefetch.store.postgres": "extra `postgres`",
}
"""Modules that need an extra, such as ``{"pkg.adapters.httpx": "extra `httpx`"}``;
without it installed their section has no signatures."""

# ---- nothing below this line changes between libraries ----------------------

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / PACKAGE / "skill" / SKILL_NAME / "references" / "api.md"

_QUALIFIED = re.compile(r"\b(?:[a-z_]\w*\.)+([A-Za-z_]\w*)")
_STRING = re.compile(r"""('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")""")
_CLASS = re.compile(r"<class '([^']+)'>")
_FUNCTION = re.compile(r"<function ([\w.<>]+) at 0x[0-9A-Fa-f]+>")
_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+")
_ROLE = re.compile(r":(?:class|meth|func|data|mod|attr|exc):`~?(?:[\w.]*\.)?([^`]+)`")
_GOOGLE_ATTRIBUTE = re.compile(r"^(\w+)(?: \(([^)]*)\))?:\s*(.*)$")
_ENUM_BASES = tuple(
    base
    for base in (enum.Enum, getattr(enum, "StrEnum", None), enum.IntEnum)
    if base is not None
)


def public_modules() -> list[str]:
    """Every importable public module of the package except ``<package>.skill``."""
    package = importlib.import_module(PACKAGE)
    found = [PACKAGE]
    for info in pkgutil.walk_packages(package.__path__, PACKAGE + "."):
        parts = info.name.split(".")
        if any(part.startswith("_") for part in parts) or parts[1:2] == ["skill"]:
            continue
        found.append(info.name)
    return sorted(found)


def _short(text: str) -> str:
    """Drop module prefixes and what changes between runs and systems.

    String literals stay as they are: in ``url='https://a.b.org/x'`` the host
    is a value, not a qualified name.
    """
    text = _CLASS.sub(r"\1", text)
    text = _FUNCTION.sub(r"\1", text)
    text = _ADDRESS.sub("", text)
    text = re.sub(r"\b(?:Posix|Windows)Path\(", "Path(", text)
    parts = _STRING.split(text)  # odd indices are the string literals
    return "".join(
        part if index % 2 else _QUALIFIED.sub(r"\1", part)
        for index, part in enumerate(parts)
    )


def _inline(text: str) -> str:
    """Turn reStructuredText inline markup into Markdown."""
    return _ROLE.sub(r"`\1`", re.sub(r"``([^`]+)``", r"`\1`", text))


def _paragraph(obj: Any) -> str:
    """The first paragraph of a docstring, on one line."""
    doc = inspect.getdoc(obj) or ""
    first = " ".join(line.strip() for line in doc.split("\n\n", 1)[0].splitlines())
    return _inline(first)


def _type(annotation: Any) -> str:
    """A type annotation with short names."""
    if isinstance(annotation, type) and not typing.get_args(annotation):
        return annotation.__name__
    return _short(annotation if isinstance(annotation, str) else str(annotation))


def _cell(text: str) -> str:
    """Make text safe inside a Markdown table cell."""
    return text.replace("|", "\\|")


def _signature(obj: Any) -> str:
    """The signature without ``self`` and, for classes, without ``-> None``."""
    try:
        signature = inspect.signature(obj, eval_str=True)
    except Exception:  # unresolvable annotations: fall back to the strings
        try:
            signature = inspect.signature(obj)
        except (TypeError, ValueError):
            return ""
    text = re.sub(r"^\((?:self|cls)(, )?", "(", _short(str(signature)))
    return text.removesuffix(" -> None") if inspect.isclass(obj) else text


def _attribute_docs(cls: type) -> dict[str, str]:
    """The string literals that follow class attributes in the source."""
    try:
        body = ast.parse(textwrap.dedent(inspect.getsource(cls))).body[0]
    except (OSError, TypeError, SyntaxError):
        return {}
    docs: dict[str, str] = {}
    if not isinstance(body, ast.ClassDef):
        return docs
    for index, node in enumerate(body.body[:-1]):
        after = body.body[index + 1]
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(after, ast.Expr)
            and isinstance(after.value, ast.Constant)
            and isinstance(after.value.value, str)
        ):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name):
                text = after.value.value.strip().split("\n\n", 1)[0]
                docs[target.id] = " ".join(text.split())
    return docs


def _docstring_attributes(cls: type) -> dict[str, tuple[str, str]]:
    """Type and meaning of each entry of ``Attributes:`` in a class docstring.

    Google style: ``name (type): meaning``, continued on deeper lines.
    """
    lines = (inspect.getdoc(cls) or "").splitlines()
    try:
        start = [line.strip() for line in lines].index("Attributes:") + 1
    except ValueError:
        return {}
    found: dict[str, tuple[str, str]] = {}
    indent = 0
    current = ""
    for line in lines[start:]:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip())
        if depth == 0:
            break  # the next section
        indent = indent or depth
        match = _GOOGLE_ATTRIBUTE.match(line.strip()) if depth == indent else None
        if match:
            current = match[1]
            found[current] = (match[2] or "", match[3])
        elif current:
            kind, text = found[current]
            found[current] = (kind, f"{text} {line.strip()}")
    return {name: (kind, _inline(text)) for name, (kind, text) in found.items()}


def _fields(cls: type) -> list[tuple[str, str, str]]:
    """Name, type and meaning of the public fields of a class.

    The fields of a dataclass, else the class annotations, plus the entries of
    the ``Attributes:`` section; the meaning comes from that section or from
    the string after the field in the source.
    """
    documented = _docstring_attributes(cls)
    after = _attribute_docs(cls)
    if dataclasses.is_dataclass(cls):
        names = [f.name for f in dataclasses.fields(cls)]
    else:
        names = list(inspect.get_annotations(cls))
    names += [name for name in documented if name not in names]
    try:
        hints = typing.get_type_hints(cls)
    except Exception:  # unresolvable annotations: fall back to the strings
        hints = inspect.get_annotations(cls)
    out = []
    for name in names:
        hint = hints.get(name)
        if name.startswith("_") or typing.get_origin(hint) is typing.ClassVar:
            continue
        kind = _type(hint) if hint is not None else documented.get(name, ("", ""))[0]
        meaning = documented[name][1] if name in documented else after.get(name, "")
        out.append((name, kind, meaning))
    return out


def _field_table(cls: type) -> list[str]:
    fields = _fields(cls)
    if not fields:
        return []
    rows = [
        f"| `{name}` | {f'`{_cell(kind)}`' if kind else ''} | {_cell(meaning)} |"
        for name, kind, meaning in fields
    ]
    return ["| Field | Type | Meaning |", "|---|---|---|", *rows, ""]


def _members(cls: type) -> list[str]:
    out = []
    for name, member in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            out.append(f"- `.{name}` (property): {_paragraph(member)}".rstrip(": "))
        elif isinstance(member, (staticmethod, classmethod)):
            func = member.__func__
            out.append(
                f"- `.{name}{_signature(func)}`: {_paragraph(func)}".rstrip(": ")
            )
        elif inspect.isfunction(member):
            out.append(
                f"- `.{name}{_signature(member)}`: {_paragraph(member)}".rstrip(": ")
            )
    return out


def _class(name: str, cls: type) -> list[str]:
    out = [f"### `{name}`", ""]
    if issubclass(cls, enum.Enum):
        out += [_paragraph(cls), ""]
        docs = _attribute_docs(cls)
        for member in cls:
            line = f"- `{member.name}` = `{member.value!r}`"
            out.append(f"{line}: {docs[member.name]}" if member.name in docs else line)
        return [*out, ""]
    if getattr(cls, "_is_protocol", False):
        return [
            *out,
            f"Protocol. {_paragraph(cls)}",
            "",
            *_field_table(cls),
            *_members(cls),
            "",
        ]
    heading = f"`{name}{_signature(cls)}`"
    bases = [
        b.__name__ for b in cls.__bases__ if b is not object and b not in _ENUM_BASES
    ]
    if bases:
        heading += f" (subclass of {', '.join(f'`{b}`' for b in bases)})"
    members = _members(cls)
    return [
        *out,
        heading,
        "",
        _paragraph(cls),
        "",
        *_field_table(cls),
        *members,
        *([""] if members else []),
    ]


def _entry(name: str, value: Any) -> list[str]:
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
        if not name.startswith("_") and hasattr(value, "_fixture_function_marker"):
            out += [f"### fixture `{name}`", "", _paragraph(value), ""]
    return out


def _loaded() -> dict[str, ModuleType]:
    """The modules of ``MODULES`` that import here: one of ``EXTRA`` may not."""
    modules = {}
    for module_name in MODULES:
        try:
            modules[module_name] = importlib.import_module(module_name)
        except ModuleNotFoundError:
            if module_name not in EXTRA:
                raise
    return modules


def exports(module: ModuleType) -> list[str]:
    """The public names of a module.

    Its ``__all__``; without one, the public classes and functions it defines,
    in the order of the source.
    """
    if hasattr(module, "__all__"):
        return list(module.__all__)
    return [
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and (inspect.isclass(value) or inspect.isfunction(value))
        and value.__module__ == module.__name__
    ]


def _homes(modules: dict[str, ModuleType]) -> dict[int, str]:
    """The module whose section describes each exported class and function.

    The module that defines it when that is one of ``modules``, else the first
    one that exports it.
    """
    homes: dict[int, str] = {}
    for module_name, module in modules.items():
        for name in exports(module):
            value = getattr(module, name)
            if inspect.isclass(value) or inspect.isfunction(value):
                defined = value.__module__
                home = defined if defined in modules else module_name
                homes.setdefault(id(value), home)
    return homes


def _entries(
    modules: dict[str, ModuleType], homes: dict[int, str]
) -> dict[str, list[tuple[str, Any]]]:
    """What each section describes, with the name it is exported under.

    First the module's own exports in their order, then what the module
    defines and only others export.
    """
    entries: dict[str, list[tuple[str, Any]]] = {name: [] for name in modules}
    seen: set[int] = set()
    for own in (True, False):
        for module_name, module in modules.items():
            for name in exports(module):
                value = getattr(module, name)
                if not (inspect.isclass(value) or inspect.isfunction(value)):
                    if own:  # constants are described where they are exported
                        entries[module_name].append((name, value))
                    continue
                home = homes[id(value)]
                if id(value) in seen or (own and home != module_name):
                    continue
                seen.add(id(value))
                entries[home].append((name, value))
    return entries


def _module(
    module_name: str,
    module: ModuleType | None,
    entries: list[tuple[str, Any]],
    homes: dict[int, str],
) -> list[str]:
    title = f"## `{module_name}`"
    if module_name in EXTRA:
        title += f" ({EXTRA[module_name]})"
    if module is None:
        # Without the extra: the section says so; tests/test_skill.py skips the
        # checks that need it, and the CI job with every extra checks them.
        note = f"Requires {EXTRA[module_name]}: install it to see the signatures."
        return [title, "", note, ""]
    out = [title, "", _paragraph(module), ""]
    elsewhere = []
    for name in exports(module):
        home = homes.get(id(getattr(module, name)), module_name)
        if home != module_name:
            elsewhere.append((name, home))
    if elsewhere:
        listed = ", ".join(f"`{name}` ({home})" for name, home in elsewhere)
        out += [f"Re-exports: {listed}.", ""]
    for name, value in entries:
        out += _entry(name, value)
    return out + _fixtures(module)


def build() -> str:
    """Return the content of ``references/api.md``."""
    modules = _loaded()
    homes = _homes(modules)
    entries = _entries(modules, homes)
    version = getattr(importlib.import_module(PACKAGE), "__version__", "")
    out = [
        f"# {PACKAGE} {version}: API reference",
        "",
        "<!-- Generated by tools/build_skill.py from the code. Do not edit by hand:",
        "     run `python tools/build_skill.py` after changing the API. -->",
        "",
        "Read only the section you need.",
        "",
        "Modules: " + " · ".join(f"`{m}`" for m in MODULES),
        "",
    ]
    for module_name in MODULES:
        module = modules.get(module_name)
        out += _module(module_name, module, entries.get(module_name, []), homes)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run ``python tools/build_skill.py``."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if stale")
    args = parser.parse_args(argv)
    content = build()
    if args.check:
        current = TARGET.read_bytes().decode("utf-8") if TARGET.exists() else ""
        if current != content:
            print(f"{TARGET} is stale: run `python tools/build_skill.py`")
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(content.encode("utf-8"))
    print(f"wrote {TARGET.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
