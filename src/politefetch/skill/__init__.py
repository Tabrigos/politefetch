"""The agent skill bundled with politefetch, and the commands that install it.

The package ships a skill (``SKILL.md`` plus ``references/``) that teaches
AI coding assistants how to use the library without reading its source. It
follows the Agent Skills format and is versioned with the package. Agents do
not look inside installed Python packages, so copy it where they look::

    python -m politefetch.skill install            # into .claude/skills/politefetch/
    python -m politefetch.skill install --target DIR
    python -m politefetch.skill check              # is the installed copy current?
    python -m politefetch.skill show               # print SKILL.md
    python -m politefetch.skill path               # where the bundled copy lives
    python -m politefetch.skill sync [--root DIR]  # every package's skill, current

Packages that ship a skill declare it in the ``agent_skills`` entry point
group, pointing at a module with a ``bundled()`` function like this one. That
is the whole contract: ``sync`` from any such package brings the skills of all
of them into ``<root>/.claude/skills/``. Run it from a ``SessionStart`` hook
and every session starts with the skills of the installed versions.

Each copy written by ``install`` or ``sync`` holds a ``.package-skill`` file
naming its distribution; ``sync`` changes only directories that have one.
"""

import argparse
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.metadata import EntryPoint, entry_points
from importlib.resources.abc import Traversable
from pathlib import Path

from politefetch import __version__

__all__ = [
    "DEFAULT_TARGET",
    "ENTRY_POINT_GROUP",
    "MARKER",
    "SKILL_NAME",
    "SkillInfo",
    "Source",
    "Status",
    "SyncReport",
    "bundled",
    "check",
    "declared",
    "discover",
    "install",
    "main",
    "read_skill",
    "skill_info",
    "sync",
]

SKILL_NAME = "politefetch"
"""Name of the skill and of its directory: the distribution name."""

ENTRY_POINT_GROUP = "agent_skills"
"""Entry point group where packages declare their skill module."""

MARKER = ".package-skill"
"""File naming the distribution in every copy written by install or sync."""

DEFAULT_TARGET = Path(".claude") / "skills" / SKILL_NAME
"""Where :func:`install` copies the skill by default (relative to the cwd)."""

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
_VERSION = re.compile(r"^\s+version:\s*[\"']?([^\"'\s]+)[\"']?\s*$", re.MULTILINE)


def bundled() -> Traversable:
    """Return the directory of the skill shipped with the package."""
    return resources.files(__package__).joinpath(SKILL_NAME)


def read_skill() -> str:
    """Return the text of the bundled ``SKILL.md``."""
    return bundled().joinpath("SKILL.md").read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """Name and version read from the frontmatter of a ``SKILL.md``.

    Attributes:
        name: The ``name`` field, or ``None``.
        version: The ``metadata.version`` field, or ``None``.
    """

    name: str | None
    version: str | None


def skill_info(text: str) -> SkillInfo:
    """Read name and version from the frontmatter of a ``SKILL.md``."""
    match = _FRONTMATTER.match(text)
    if match is None:
        return SkillInfo(None, None)
    head = match[1]
    name = _NAME.search(head)
    version = _VERSION.search(head)
    return SkillInfo(name[1] if name else None, version[1] if version else None)


def _copy(source: Traversable, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_dir():
            _copy(item, target / item.name)
        elif not item.name.endswith((".pyc", ".pyo")):
            (target / item.name).write_bytes(item.read_bytes())


def _write(source: Traversable, target: Path, distribution: str) -> None:
    """Replace ``target`` with a marked copy of ``source``."""
    if target.exists():
        shutil.rmtree(target)
    _copy(source, target)
    (target / MARKER).write_text(f"{distribution}\n", encoding="utf-8")


def _marker(directory: Path) -> str | None:
    """Return the distribution named by the marker in ``directory``, if any."""
    path = directory / MARKER
    return path.read_text("utf-8").strip() if path.is_file() else None


def _version(directory: Traversable) -> str | None:
    path = directory.joinpath("SKILL.md")
    return skill_info(path.read_text("utf-8")).version if path.is_file() else None


def install(target: Path = DEFAULT_TARGET, *, force: bool = False) -> Path:
    """Copy the bundled skill into ``target``.

    A previous copy of this skill in ``target`` is replaced. Any other
    non-empty directory is left alone unless ``force`` is true. The copy gets
    the ``.package-skill`` marker but no ``.gitignore``: it is the project's
    to version if it wants.

    Args:
        target: Directory to create or replace.
        force: Replace ``target`` even if it is not a copy of this skill.

    Returns:
        The directory written.

    Raises:
        FileExistsError: If ``target`` holds something else and ``force`` is
            false.
    """
    if target.exists():
        current = target / "SKILL.md"
        ours = _marker(target) == SKILL_NAME or (
            current.is_file()
            and skill_info(current.read_text("utf-8")).name == SKILL_NAME
        )
        if not ours and any(target.iterdir()) and not force:
            raise FileExistsError(
                f"{target} exists and is not a copy of the {SKILL_NAME} skill;"
                " use --force to replace it"
            )
    _write(bundled(), target, SKILL_NAME)
    return target


@dataclass(frozen=True, slots=True)
class Status:
    """State of an installed copy compared with the bundled skill.

    Attributes:
        current: Whether the installed copy matches the package version.
        installed: Version of the installed copy, or ``None`` if missing.
        bundled: Version of the skill shipped with the package.
        message: One line for humans.
    """

    current: bool
    installed: str | None
    bundled: str | None
    message: str


def check(target: Path = DEFAULT_TARGET) -> Status:
    """Compare the copy in ``target`` with the bundled skill."""
    shipped = skill_info(read_skill()).version
    path = target / "SKILL.md"
    if not path.is_file():
        return Status(False, None, shipped, f"not installed in {target}")
    installed = skill_info(path.read_text("utf-8")).version
    if installed != shipped:
        return Status(
            False,
            installed,
            shipped,
            f"{target} has version {installed}, the package has {shipped}:"
            " run `python -m politefetch.skill install`",
        )
    return Status(True, installed, shipped, f"{target} is current ({installed})")


@dataclass(frozen=True, slots=True)
class Source:
    """A skill shipped by an installed package.

    Attributes:
        distribution: Normalised distribution name, also the directory name.
        directory: The skill directory inside the package.
    """

    distribution: str
    directory: Traversable


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution(point: EntryPoint) -> str:
    return _normalise(point.dist.name if point.dist else point.name)


def declared() -> set[str]:
    """Return the distributions declaring a skill, whether it loads or not."""
    return {_distribution(point) for point in entry_points(group=ENTRY_POINT_GROUP)}


def discover() -> tuple[list[Source], list[str]]:
    """Find the skills of the installed packages (``agent_skills`` entry points).

    Returns:
        The skills found, and one warning per entry point that failed to load.
    """
    sources: dict[str, Source] = {}
    warnings: list[str] = []
    for point in entry_points(group=ENTRY_POINT_GROUP):
        distribution = _distribution(point)
        try:
            directory = point.load().bundled()
        except Exception as exc:  # any broken package must not stop the others
            warnings.append(f"skipped the skill of {point.name}: {exc!r}")
            continue
        sources.setdefault(distribution, Source(distribution, directory))
    return list(sources.values()), warnings


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What :func:`sync` did.

    Attributes:
        entries: One item per skill, such as ``"politefetch 0.2.0 (updated)"``,
            sorted by name.
        warnings: Problems met; the skills they concern were left as they were.
    """

    entries: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def summary(self) -> str:
        """One line for the session log: ``skills: a 1.0 (added), b 2.0``."""
        return "skills: " + (", ".join(self.entries) or "none")


def sync(
    root: Path = Path(),
    sources: Iterable[Source] | None = None,
    *,
    keep: Iterable[str] | None = None,
) -> SyncReport:
    """Make ``<root>/.claude/skills/`` hold the skills of the installed packages.

    A missing skill is copied, with a ``.gitignore`` that ignores the copy. A
    copy whose version differs is replaced, keeping its ``.gitignore`` if it
    had one. A marked copy whose package is gone is removed; the copy of a
    package that is installed but fails to load is kept. Directories without
    the ``.package-skill`` marker are never touched.

    Args:
        root: Project directory.
        sources: Skills to sync; by default those found by :func:`discover`.
        keep: Distributions whose copies are never removed, besides those in
            ``sources``; by default, with ``sources`` omitted, every
            distribution that :func:`declared` lists.

    Returns:
        The summary and the warnings; problems never raise.
    """
    warnings: list[str] = []
    if sources is None:
        found, warnings = discover()
        sources = found
        if keep is None:
            keep = declared()
    skills = root / ".claude" / "skills"
    entries: dict[str, str] = {}
    known: set[str] = set(keep or ())
    for source in sources:
        known.add(source.distribution)
        name = source.directory.name
        target = skills / name
        try:
            shipped = _version(source.directory)
            if shipped is None:
                raise FileNotFoundError(f"no SKILL.md in {source.directory}")
            if not target.exists():
                _write(source.directory, target, source.distribution)
                (target / ".gitignore").write_text("*\n", encoding="utf-8")
                entries[name] = f"{name} {shipped} (added)"
            elif _marker(target) is None:
                warnings.append(
                    f"left {target} alone: it has no {MARKER} file, so it was not"
                    " written by sync or install"
                )
            elif _version(target) == shipped:
                entries[name] = f"{name} {shipped}"
            else:
                gitignore = target / ".gitignore"
                kept = gitignore.read_bytes() if gitignore.is_file() else None
                _write(source.directory, target, source.distribution)
                if kept is not None:
                    gitignore.write_bytes(kept)
                entries[name] = f"{name} {shipped} (updated)"
        except Exception as exc:  # report and go on with the other skills
            warnings.append(f"could not sync the skill of {source.distribution}: {exc}")
    try:
        directories = sorted(skills.iterdir()) if skills.is_dir() else []
        for directory in directories:
            owner = _marker(directory) if directory.is_dir() else None
            if owner is not None and owner not in known:
                shutil.rmtree(directory)
                entries[directory.name] = f"{directory.name} (removed)"
    except Exception as exc:
        warnings.append(f"could not clean {skills}: {exc}")
    return SyncReport(tuple(entries[name] for name in sorted(entries)), tuple(warnings))


def _run_sync(root: Path) -> int:
    try:
        report = sync(root)
    except Exception as exc:  # a session must never fail because of a skill
        report = SyncReport((), (f"sync failed: {exc!r}",))
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(report.summary)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``python -m politefetch.skill`` command line."""
    parser = argparse.ArgumentParser(
        prog="python -m politefetch.skill",
        description=f"Manage the agent skill of politefetch {__version__}.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("install", "copy the skill into a skills directory"),
        ("check", "say whether the installed copy matches this version"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "--target",
            type=Path,
            default=DEFAULT_TARGET,
            help=f"skill directory (default: {DEFAULT_TARGET})",
        )
        if name == "install":
            command.add_argument(
                "--force", action="store_true", help="replace any existing directory"
            )
    commands.add_parser("show", help="print SKILL.md")
    commands.add_parser("path", help="print where the bundled skill lives")
    command = commands.add_parser(
        "sync",
        help="install or update the skills of every installed package; one line",
    )
    command.add_argument(
        "--root",
        type=Path,
        default=Path(),
        help="project directory (default: the current directory)",
    )
    args = parser.parse_args(argv)

    if args.command == "show":
        sys.stdout.write(read_skill())
        return 0
    if args.command == "path":
        print(bundled())
        return 0
    if args.command == "sync":
        return _run_sync(args.root)
    if args.command == "install":
        try:
            written = install(args.target, force=args.force)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"installed the {SKILL_NAME} skill {__version__} in {written}")
        return 0
    status = check(args.target)
    print(status.message)
    return 0 if status.current else 1
