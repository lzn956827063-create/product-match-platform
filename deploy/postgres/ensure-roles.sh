#!/bin/sh
set -eu

export PGPASSWORD="$(cat "$PG_BOOTSTRAP_PASSWORD_FILE")"
owner_password="$(cat "$DB_OWNER_PASSWORD_FILE")"
app_password="$(cat "$DB_APP_PASSWORD_FILE")"
readonly_password="$(cat "$DB_READONLY_PASSWORD_FILE")"
backup_password="$(cat "$DB_BACKUP_PASSWORD_FILE")"

psql --no-psqlrc --set ON_ERROR_STOP=1 \
  --set db_name="$PGDATABASE" \
  --set owner_password="$owner_password" \
  --set app_password="$app_password" \
  --set readonly_password="$readonly_password" \
  --set backup_password="$backup_password" <<'SQL'
SELECT format('CREATE ROLE productmatch_owner LOGIN PASSWORD %L', :'owner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'productmatch_owner') \gexec
SELECT format('CREATE ROLE productmatch_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'productmatch_app') \gexec
SELECT format('CREATE ROLE productmatch_readonly LOGIN PASSWORD %L', :'readonly_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'productmatch_readonly') \gexec
SELECT format('CREATE ROLE productmatch_backup LOGIN PASSWORD %L', :'backup_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'productmatch_backup') \gexec

SELECT format('ALTER ROLE productmatch_owner PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'owner_password') \gexec
SELECT format('ALTER ROLE productmatch_app PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'app_password') \gexec
SELECT format('ALTER ROLE productmatch_readonly PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'readonly_password') \gexec
SELECT format('ALTER ROLE productmatch_backup PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'backup_password') \gexec

SELECT format('ALTER DATABASE %I OWNER TO productmatch_owner', :'db_name') \gexec
ALTER SCHEMA public OWNER TO productmatch_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db_name') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO productmatch_owner, productmatch_app, productmatch_readonly, productmatch_backup', :'db_name') \gexec
GRANT USAGE ON SCHEMA public TO productmatch_app, productmatch_readonly, productmatch_backup;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO productmatch_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO productmatch_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO productmatch_readonly, productmatch_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO productmatch_readonly, productmatch_backup;

ALTER DEFAULT PRIVILEGES FOR ROLE productmatch_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO productmatch_app;
ALTER DEFAULT PRIVILEGES FOR ROLE productmatch_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO productmatch_app;
ALTER DEFAULT PRIVILEGES FOR ROLE productmatch_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO productmatch_readonly, productmatch_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE productmatch_owner IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO productmatch_readonly, productmatch_backup;

ALTER ROLE productmatch_app SET timezone = 'UTC';
ALTER ROLE productmatch_app SET statement_timeout = '30s';
ALTER ROLE productmatch_app SET lock_timeout = '5s';
ALTER ROLE productmatch_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE productmatch_readonly SET default_transaction_read_only = on;
ALTER ROLE productmatch_backup SET default_transaction_read_only = on;
GRANT pg_monitor TO productmatch_readonly;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL
