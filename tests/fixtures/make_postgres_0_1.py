"""Write `postgres-0.1.1.sql`, the tables the Postgres store of 0.1.1 creates.

Run it from the repository root, with the tag fetched:

    python -I tests/fixtures/make_postgres_0_1.py > tests/fixtures/postgres-0.1.1.sql

It reads the `_TABLES` statements from the source at the tag `v0.1.1`, without
importing it, so it needs neither psycopg nor that version installed.
"""

import ast
import subprocess

source = subprocess.run(
    ["git", "show", "v0.1.1:src/politefetch/store/postgres.py"],
    capture_output=True,
    text=True,
    check=True,
).stdout
for node in ast.parse(source).body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "_TABLES"
        for target in node.targets
    ):
        statements = ast.literal_eval(node.value)
        break
else:
    raise SystemExit("no _TABLES in the 0.1.1 source")
print("-- The tables of the Postgres store of politefetch 0.1.1, statement by")
print("-- statement; written by make_postgres_0_1.py from the tag v0.1.1.")
for statement in statements:
    print(" ".join(statement.split()) + ";")
