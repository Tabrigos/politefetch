"""The agent skill shipped inside this package, and the command that serves it.

The skill (``SKILL.md`` plus ``references/``) sits next to this module and is
versioned with the package. Coding agents do not read installed packages, so a
project copies it into its own ``.claude/skills/<name>/`` and keeps the copy in
git, where every change shows up in a diff::

    python -m <package>.skill path      # directory of the bundled skill
    python -m <package>.skill show      # print SKILL.md
    python -m <package>.skill check     # exit 1 if the project copy differs
    python -m <package>.skill install --target .claude/skills/<name>

Nothing here writes unless ``install`` is called with an explicit target, and
``install`` only writes ``.claude/skills/<name>`` or ``.agents/skills/<name>``.
Copies carry a ``.skill-libreria.json`` marker with the content hash; copies
are compared by hash with line endings normalised, never by version.

Schema 1 of the library skill. This file is a copy of the shared template:
only the constants above the line change from one library to another, and
``tests/test_skill.py`` checks the fingerprint of everything below it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from importlib import resources
from pathlib import Path

DISTRIBUTION = "politefetch"
"""The name on the package index, as projects list it."""

SKILL_NAME = "politefetch"
"""Name of the skill and of its directory."""

# ---- nothing below this line changes between libraries ----------------------

__all__ = [
    "DEFAULT_TARGET",
    "DISTRIBUTION",
    "MARKER",
    "SCHEMA",
    "SKILL_NAME",
    "bundled",
    "check",
    "content_hash",
    "install",
    "main",
    "read_skill",
    "skill_version",
]

SCHEMA = 1
MARKER = ".skill-libreria.json"
DEFAULT_TARGET = Path(".claude") / "skills" / SKILL_NAME
_PARENTS = ((".claude", "skills"), (".agents", "skills"))
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_VERSION = re.compile(
    r"^metadata:[ \t]*\n(?:[ \t]+.*\n)*?[ \t]+version:[ \t]*[\"']?([^\"'\s]+)",
    re.MULTILINE,
)


def bundled() -> Path:
    """Return the directory of the skill shipped with the package."""
    directory = Path(str(resources.files(__package__).joinpath(SKILL_NAME)))
    if not (directory / "SKILL.md").is_file():
        raise FileNotFoundError(f"no SKILL.md in {directory}: built without the skill")
    return directory


def read_skill() -> str:
    """Return the text of the bundled ``SKILL.md``."""
    return (bundled() / "SKILL.md").read_text(encoding="utf-8")


def skill_version(text: str) -> str | None:
    """Return ``metadata.version`` from the frontmatter of a ``SKILL.md``."""
    match = _FRONTMATTER.match(text.replace("\r\n", "\n"))
    version = _VERSION.search(match[1] + "\n") if match else None
    return version[1] if version else None


def _files(directory: Path) -> list[str]:
    out = []
    for path in directory.rglob("*"):
        rel = path.relative_to(directory).as_posix()
        if not path.is_file() or rel == MARKER or "__pycache__" in rel.split("/"):
            continue
        if not rel.endswith((".pyc", ".pyo")):
            out.append(rel)
    return sorted(out)


def content_hash(directory: Path) -> str:
    """Hash a skill directory, the marker left out.

    For each file in order: its path, NUL, its content with CRLF turned into
    LF, NUL.
    """
    digest = hashlib.sha256()
    for rel in _files(directory):
        data = (directory / rel).read_bytes().replace(b"\r\n", b"\n")
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    return "sha256:" + digest.hexdigest()


def _marker(directory: Path) -> dict[str, object] | None:
    path = directory / MARKER
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def install(target: Path) -> Path:
    """Copy the bundled skill into ``target`` and mark the copy.

    Raises:
        ValueError: If ``target`` is not ``.claude/skills/<name>`` or
            ``.agents/skills/<name>``.
        FileExistsError: If ``target`` holds files that are not a marked copy
            of this package's skill; nothing is removed then.
    """
    parts = target.resolve().parts
    if parts[-1:] != (SKILL_NAME,) or tuple(parts[-3:-1]) not in _PARENTS:
        raise ValueError(
            f"{target} is not .claude/skills/{SKILL_NAME}"
            f" or .agents/skills/{SKILL_NAME}"
        )
    if target.exists() and any(target.iterdir()):
        marker = _marker(target)
        if marker is None or marker.get("pacchetto") != DISTRIBUTION:
            raise FileExistsError(
                f"{target} is not a copy of the {DISTRIBUTION} skill"
                f" (no {MARKER}): left alone"
            )
        shutil.rmtree(target)
    source = bundled()
    for rel in _files(source):
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / rel).read_bytes())
    marker = {
        "schema": SCHEMA,
        "pacchetto": DISTRIBUTION,
        "ecosistema": "python",
        "versione": skill_version(read_skill()),
        "hash": content_hash(target),
    }
    text = json.dumps(marker, indent=2, ensure_ascii=False) + "\n"
    (target / MARKER).write_bytes(text.encode("utf-8"))
    return target


def _find_copy() -> Path:
    """The copy from the current directory up to the one with ``.git``.

    The first ``.claude/skills/<name>`` or ``.agents/skills/<name>`` with a
    ``SKILL.md``; without one, ``.claude/skills/<name>`` at the root of the
    repository, or in the current directory outside a repository.
    """
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        for parent in _PARENTS:
            copy = directory.joinpath(*parent, SKILL_NAME)
            if (copy / "SKILL.md").is_file():
                return copy
        if (directory / ".git").exists():
            return directory / DEFAULT_TARGET
    return DEFAULT_TARGET


def check(target: Path | None = None) -> tuple[bool, str]:
    """Compare the copy in ``target`` with the bundled skill, by hash.

    Without ``target``, the copy is looked for from the current directory up
    to the root of the repository.
    """
    target = _find_copy() if target is None else target
    version = skill_version(read_skill())
    if not (target / "SKILL.md").is_file():
        return False, (
            f"no copy in {target}:"
            f" python -m {__package__} install --target {target.as_posix()}"
        )
    copy = content_hash(target)
    marker = _marker(target)
    if marker is not None and marker.get("hash") != copy:
        return False, f"{target} was edited by hand: send the fix to the library"
    if copy != content_hash(bundled()):
        return False, (
            f"{target} differs from the installed {DISTRIBUTION} {version}:"
            " copy it again and review the diff"
        )
    return True, f"{target} matches {DISTRIBUTION} {version}"


def main(argv: list[str] | None = None) -> int:
    """Run ``python -m <package>.skill``."""
    parser = argparse.ArgumentParser(
        prog=f"python -m {__package__}", description="The agent skill of the package."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("path", help="print the directory of the bundled skill")
    commands.add_parser("show", help="print SKILL.md")
    command = commands.add_parser("check", help="exit 1 if the copy differs")
    command.add_argument("--target", type=Path)
    command = commands.add_parser("install", help="copy into .claude/skills/<name>")
    command.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "path":
        print(bundled())
        return 0
    if args.command == "show":
        sys.stdout.write(read_skill())
        return 0
    if args.command == "install":
        try:
            written = install(args.target)
        except (ValueError, FileExistsError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"copied the {SKILL_NAME} skill into {written}: review the diff")
        return 0
    current, message = check(args.target)
    print(message)
    return 0 if current else 1
