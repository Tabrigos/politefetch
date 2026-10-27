"""Schema versions of the stores that keep their state in a database.

Version 1 is the schema of politefetch 0.1, which recorded no version: a
database with the tables and no version is at version 1. Each later version is
one migration, SQL statements run in order in the same transaction as the
version update, so a migration that fails leaves the database as it was.

Migrations only add (tables, columns with a default, indexes): a process still
running the older version keeps applying the rules it knows. A database newer
than the running code is refused, because that code would ignore state that
may hold requests back.
"""

from collections.abc import Sequence

__all__ = ["BASE_VERSION", "VERSION_TABLE", "pending"]

BASE_VERSION = 1

VERSION_TABLE = "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"


def pending(
    found: int, migrations: Sequence[Sequence[str]], where: str
) -> list[tuple[int, Sequence[str]]]:
    """The migrations still to run on a database at version `found`, each with
    the version it leads to; `migrations[0]` leads to version 2."""
    latest = BASE_VERSION + len(migrations)
    if found > latest:
        raise ValueError(
            f"{where} has schema version {found}, newer than this politefetch"
            f" knows ({latest}): upgrade politefetch. An older version would"
            " ignore state that holds requests back"
        )
    return [
        (version, migrations[version - BASE_VERSION - 1])
        for version in range(found + 1, latest + 1)
    ]
