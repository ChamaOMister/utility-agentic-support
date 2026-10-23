#!/bin/bash
# Runs once, as the Postgres superuser, on first start of an empty data directory.
# Creates the two service roles, the uas and uas_test databases, and in each database
# the support and billing schemas (owned by their role, closed to PUBLIC).
set -euo pipefail

: "${SUPPORT_DB_PASSWORD:?}" "${BILLING_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v support_pw="$SUPPORT_DB_PASSWORD" -v billing_pw="$BILLING_DB_PASSWORD" <<'SQL'
CREATE ROLE support_app LOGIN PASSWORD :'support_pw';
CREATE ROLE billing_app LOGIN PASSWORD :'billing_pw';
CREATE DATABASE uas;
CREATE DATABASE uas_test;
SQL

for db in uas uas_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
CREATE SCHEMA support AUTHORIZATION support_app;
CREATE SCHEMA billing AUTHORIZATION billing_app;
REVOKE ALL ON SCHEMA billing FROM PUBLIC;
REVOKE ALL ON SCHEMA support FROM PUBLIC;
SQL
done
