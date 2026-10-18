"""``python -m politefetch.skill sync`` keeps a project's package skills current.

Every package that ships a skill declares it in the ``agent_skills`` entry
point group. ``sync`` copies each one into ``<root>/.claude/skills/<name>/``,
updates copies whose version changed, removes copies of packages that are
gone, and never touches a directory it did not write.
"""

import json
import re
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

import pytest

import politefetch
import politefetch.skill
from politefetch.skill import (
    ENTRY_POINT_GROUP,
    MARKER,
    SKILL_NAME,
    Source,
    declared,
    discover,
    install,
    main,
    sync,
)

README = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


def _package(base: Path, name: str, version: str) -> Source:
    """A skill directory as a package would ship it."""
    directory = base / "packages" / name / version / name
    (directory / "references").mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: Test.\nmetadata:\n  version: "{version}"\n'
        f"---\n\n# {name} {version}\n",
        encoding="utf-8",
    )
    (directory / "references" / "api.md").write_text(version, encoding="utf-8")
    return Source(name, directory)


def _skills(root: Path) -> Path:
    return root / ".claude" / "skills"


# --- discovery ----------------------------------------------------------------


def test_entry_point_is_in_the_installed_metadata() -> None:
    found = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert found[SKILL_NAME].value == "politefetch.skill"
    assert found[SKILL_NAME].load() is politefetch.skill


def test_discover_finds_this_package() -> None:
    sources, warnings = discover()
    assert warnings == []
    ours = [s for s in sources if s.distribution == SKILL_NAME]
    assert len(ours) == 1
    assert ours[0].directory.name == SKILL_NAME
    assert ours[0].directory.joinpath("SKILL.md").is_file()
    assert SKILL_NAME in declared()


def test_discover_skips_broken_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = EntryPoint("broken", "no_such_module_for_politefetch", ENTRY_POINT_GROUP)
    monkeypatch.setattr(politefetch.skill, "entry_points", lambda group: [broken])
    sources, warnings = discover()
    assert sources == []
    assert len(warnings) == 1
    assert "broken" in warnings[0]


# --- sync ---------------------------------------------------------------------


def test_sync_adds_skills_with_marker_and_gitignore(tmp_path: Path) -> None:
    report = sync(tmp_path, [_package(tmp_path, "alpha", "1.0")])
    target = _skills(tmp_path) / "alpha"
    assert "# alpha 1.0" in (target / "SKILL.md").read_text("utf-8")
    assert (target / "references" / "api.md").read_text("utf-8") == "1.0"
    assert (target / MARKER).read_text("utf-8") == "alpha\n"
    assert (target / ".gitignore").read_text("utf-8") == "*\n"
    assert report.summary == "skills: alpha 1.0 (added)"
    assert report.warnings == ()


def test_sync_leaves_current_copies_alone(tmp_path: Path) -> None:
    source = _package(tmp_path, "alpha", "1.0")
    sync(tmp_path, [source])
    note = _skills(tmp_path) / "alpha" / "note.txt"
    note.write_text("untouched", encoding="utf-8")
    report = sync(tmp_path, [source])
    assert note.read_text("utf-8") == "untouched"
    assert report.summary == "skills: alpha 1.0"


def test_sync_updates_a_changed_version(tmp_path: Path) -> None:
    sync(tmp_path, [_package(tmp_path, "alpha", "1.0")])
    target = _skills(tmp_path) / "alpha"
    (target / "note.txt").write_text("stale", encoding="utf-8")
    report = sync(tmp_path, [_package(tmp_path, "alpha", "1.1")])
    assert "# alpha 1.1" in (target / "SKILL.md").read_text("utf-8")
    assert not (target / "note.txt").exists()
    assert (target / MARKER).is_file()
    assert (target / ".gitignore").read_text("utf-8") == "*\n"
    assert report.summary == "skills: alpha 1.1 (updated)"


def test_sync_keeps_a_copy_out_of_gitignore_if_it_was(tmp_path: Path) -> None:
    """A copy made by ``install`` is versioned by the project: keep it so."""
    install(_skills(tmp_path) / SKILL_NAME)
    old = _skills(tmp_path) / SKILL_NAME / "SKILL.md"
    old.write_text(
        old.read_text("utf-8").replace(politefetch.__version__, "0.0.1"), "utf-8"
    )
    report = sync(tmp_path)
    assert f"{SKILL_NAME} {politefetch.__version__} (updated)" in report.summary
    assert not (_skills(tmp_path) / SKILL_NAME / ".gitignore").exists()


def test_sync_keeps_a_customised_gitignore(tmp_path: Path) -> None:
    sync(tmp_path, [_package(tmp_path, "alpha", "1.0")])
    gitignore = _skills(tmp_path) / "alpha" / ".gitignore"
    gitignore.write_text("references/\n", encoding="utf-8")
    sync(tmp_path, [_package(tmp_path, "alpha", "2.0")])
    assert gitignore.read_text("utf-8") == "references/\n"


def test_sync_removes_skills_of_uninstalled_packages(tmp_path: Path) -> None:
    sync(
        tmp_path, [_package(tmp_path, "alpha", "1.0"), _package(tmp_path, "beta", "2")]
    )
    report = sync(tmp_path, [_package(tmp_path, "beta", "2")])
    assert not (_skills(tmp_path) / "alpha").exists()
    assert (_skills(tmp_path) / "beta").is_dir()
    assert report.summary == "skills: alpha (removed), beta 2"


def test_sync_keeps_the_skill_of_a_package_that_fails_to_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed but broken (say, a half-done upgrade): keep the old copy."""
    sync(tmp_path, [_package(tmp_path, "alpha", "1.0")])
    broken = EntryPoint("alpha", "no_such_module_for_politefetch", ENTRY_POINT_GROUP)
    monkeypatch.setattr(politefetch.skill, "entry_points", lambda group: [broken])
    assert declared() == {"alpha"}
    report = sync(tmp_path)
    assert (_skills(tmp_path) / "alpha" / "SKILL.md").is_file()
    assert report.summary == "skills: none"
    assert len(report.warnings) == 1
    assert "alpha" in report.warnings[0]


def test_sync_keep_protects_copies_from_removal(tmp_path: Path) -> None:
    sync(tmp_path, [_package(tmp_path, "alpha", "1.0")])
    report = sync(tmp_path, [], keep={"alpha"})
    assert (_skills(tmp_path) / "alpha").is_dir()
    assert report.summary == "skills: none"


def test_sync_leaves_unmarked_directories_alone(tmp_path: Path) -> None:
    mine = _skills(tmp_path) / "alpha"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own skill", encoding="utf-8")
    other = _skills(tmp_path) / "notes"
    other.mkdir()
    report = sync(tmp_path, [_package(tmp_path, "alpha", "1.0")])
    assert (mine / "SKILL.md").read_text("utf-8") == "my own skill"
    assert other.is_dir()
    assert report.summary == "skills: none"
    assert len(report.warnings) == 1
    assert "alpha" in report.warnings[0]
    assert MARKER in report.warnings[0]


def test_sync_summary_is_one_sorted_line(tmp_path: Path) -> None:
    sources = [_package(tmp_path, n, "1") for n in ("gamma", "alpha", "beta")]
    report = sync(tmp_path, sources)
    assert report.summary == (
        "skills: alpha 1 (added), beta 1 (added), gamma 1 (added)"
    )
    assert "\n" not in report.summary
    assert sync(tmp_path / "empty", []).summary == "skills: none"


def test_sync_reports_a_problem_and_goes_on(tmp_path: Path) -> None:
    missing = Source("ghost", tmp_path / "nowhere")
    report = sync(tmp_path, [missing, _package(tmp_path, "alpha", "1.0")])
    assert report.summary == "skills: alpha 1.0 (added)"
    assert len(report.warnings) == 1
    assert "ghost" in report.warnings[0]


# --- the command --------------------------------------------------------------


def test_sync_command_installs_this_skill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["sync", "--root", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert out.count("\n") == 1
    assert f"{SKILL_NAME} {politefetch.__version__} (added)" in out
    assert err == ""
    target = _skills(tmp_path) / SKILL_NAME
    assert (target / "SKILL.md").read_text("utf-8") == politefetch.skill.read_skill()
    assert (target / MARKER).read_text("utf-8") == f"{SKILL_NAME}\n"


def test_sync_command_defaults_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["sync"]) == 0
    capsys.readouterr()
    assert (_skills(tmp_path) / SKILL_NAME / "SKILL.md").is_file()


def test_sync_command_never_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "skills").write_text("not a directory", encoding="utf-8")
    assert main(["sync", "--root", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert out.count("\n") == 1
    assert out.startswith("skills:")
    assert err.strip()


def test_install_writes_the_marker_but_no_gitignore(tmp_path: Path) -> None:
    target = install(tmp_path / SKILL_NAME)
    assert (target / MARKER).read_text("utf-8") == f"{SKILL_NAME}\n"
    assert not (target / ".gitignore").exists()


# --- documentation ------------------------------------------------------------


def test_readme_hook_runs_sync() -> None:
    blocks = re.findall(r"^```json\n(.*?)^```$", README, re.MULTILINE | re.DOTALL)
    hooks = [json.loads(block) for block in blocks if "SessionStart" in block]
    assert len(hooks) == 1
    (entry,) = hooks[0]["hooks"]["SessionStart"]
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"].endswith("python -m politefetch.skill sync")
    assert "$CLAUDE_PROJECT_DIR" in hook["command"]
