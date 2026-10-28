"""Every public field in the API reference says what it means.

The generator (a copy of the shared template) takes the meaning from an
`Attributes:` section of the class docstring or from the string after the
field; a field with neither gets an empty cell, and a reader has to guess
what, say, `throttled` counts.
"""

import re

import build_skill

ROW = re.compile(r"^\| `(\w+)` \| (.*?) \| (.*) \|$")


def field_rows(reference: str) -> list[tuple[str, str, str]]:
    """Class, field and meaning of every row of the field tables."""
    rows = []
    heading = ""
    for line in reference.splitlines():
        if line.startswith("### "):
            heading = line.removeprefix("### ").strip("`")
        match = ROW.match(line)
        if match:
            rows.append((heading, match[1], match[3].strip()))
    return rows


def test_the_rows_are_found() -> None:
    rows = field_rows(build_skill.build())
    classes = {cls for cls, _, _ in rows}
    assert {"HostStatus", "RequestRow", "Outcome", "HostPolicy"} <= classes
    assert len(rows) >= 100


def test_every_public_field_has_a_meaning() -> None:
    empty = [
        f"{cls}.{field}"
        for cls, field, meaning in field_rows(build_skill.build())
        if not meaning
    ]
    assert not empty, f"fields without a meaning in api.md: {', '.join(empty)}"


def test_an_empty_meaning_is_seen() -> None:
    reference = "### `Thing`\n\n| Field | Type | Meaning |\n|---|---|---|\n"
    reference += "| `size` | `int` |  |\n| `name` | `str` | What it is called. |\n"
    assert field_rows(reference) == [
        ("Thing", "size", ""),
        ("Thing", "name", "What it is called."),
    ]
