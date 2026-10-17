#!/usr/bin/env bash
# Start the local PostgreSQL 16 cluster and create a role and a database for
# the test suite, then print the variable the tests read.
#
#   eval "$(scripts/postgres-for-tests.sh)"
#   uv run pytest
#
# The password is a throwaway for a database on loopback, not a secret.
set -euo pipefail

pg_ctlcluster 16 main start >/dev/null 2>&1 || true
until pg_isready -q -h 127.0.0.1 -p 5432; do sleep 0.2; done

as_postgres() { su postgres -c "psql -v ON_ERROR_STOP=1 -qtAc \"$1\""; }

if [ -z "$(as_postgres "SELECT 1 FROM pg_roles WHERE rolname = 'politefetch'")" ]; then
    as_postgres "CREATE ROLE politefetch LOGIN PASSWORD 'politefetch'" >/dev/null
fi
if [ -z "$(as_postgres "SELECT 1 FROM pg_database WHERE datname = 'politefetch_test'")" ]; then
    as_postgres "CREATE DATABASE politefetch_test OWNER politefetch" >/dev/null
fi

echo "export POLITEFETCH_TEST_POSTGRES='host=127.0.0.1 port=5432 dbname=politefetch_test user=politefetch password=politefetch'"
