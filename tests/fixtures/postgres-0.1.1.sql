-- The tables of the Postgres store of politefetch 0.1.1, statement by
-- statement; written by make_postgres_0_1.py from the tag v0.1.1.
CREATE TABLE IF NOT EXISTS hosts ( host text PRIMARY KEY, not_before timestamptz, not_before_reason text, failures bigint NOT NULL DEFAULT 0, throttled bigint NOT NULL DEFAULT 0, freeze_since timestamptz, freeze_until timestamptz, freeze_status integer, freeze_note text );
CREATE TABLE IF NOT EXISTS daily_counts ( host text NOT NULL, day date NOT NULL, requests bigint NOT NULL, wire_bytes bigint NOT NULL DEFAULT 0, decoded_bytes bigint NOT NULL DEFAULT 0, PRIMARY KEY (host, day) );
CREATE TABLE IF NOT EXISTS requests ( id bigserial PRIMARY KEY, host text NOT NULL, url text NOT NULL, opened_at timestamptz NOT NULL, state text NOT NULL, closed_at timestamptz, ok boolean, status integer, failure text, reserved_bytes bigint NOT NULL DEFAULT 0, wire_bytes bigint, decoded_bytes bigint );
CREATE INDEX IF NOT EXISTS requests_by_state ON requests (state, opened_at);
CREATE INDEX IF NOT EXISTS requests_by_host ON requests (host, state);
CREATE INDEX IF NOT EXISTS requests_latest ON requests (host, id);
CREATE TABLE IF NOT EXISTS unfreezes ( id bigserial PRIMARY KEY, host text NOT NULL, at timestamptz NOT NULL, by_whom text NOT NULL, note text NOT NULL, freeze_since timestamptz NOT NULL, freeze_until timestamptz, freeze_status integer, freeze_note text NOT NULL );
CREATE TABLE IF NOT EXISTS urls ( url text PRIMARY KEY, etag text, last_modified text, size bigint, updated_at timestamptz NOT NULL );
CREATE TABLE IF NOT EXISTS data_times ( url text PRIMARY KEY, latest timestamptz NOT NULL, reads bigint NOT NULL, updated_at timestamptz NOT NULL, since timestamptz );
ALTER TABLE data_times ADD COLUMN IF NOT EXISTS since timestamptz;
