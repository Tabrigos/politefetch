"""Write `postgres-0.1.1.sql`, the tables the Postgres store of 0.1.1 creates.

Run it from the repository root, with the tag fetched, giving it the source
of the store at that tag:

    git show v0.1.1:src/politefetch/store/postgres.py \
        | python -I tests/fixtures/make_postgres_0_1.py \
        > tests/fixtures/postgres-0.1.1.sql

It reads the `_TABLES` statements from that source without importing it, so
it needs neither psycopg nor that version installed.
"""

import ast
import sys

source = sys.stdin.read()
for node in ast.parse(source).body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "_TABLES"
        for target in node.targets
    ):
        statements = ast.literal_eval(node.value)
        break
else:
    raise SystemExit("no _TABLES in the source given")
print("-- The tables of the Postgres store of politefetch 0.1.1, statement by")
print("-- statement; written by make_postgres_0_1.py from the tag v0.1.1.")
for statement in statements:
    print(" ".join(statement.split()) + ";")
