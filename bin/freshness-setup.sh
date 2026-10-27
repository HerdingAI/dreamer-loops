#!/usr/bin/env bash
# One-time setup for the freshness display.
# Creates the database named by PGDATABASE on the Postgres server described in
# ~/.config/dreamer/postgres.env (override: DREAMER_PG_ENV) if it does not
# exist, publishes one snapshot
# (which creates schema `health`), and reads it back.
#
# This is the only step that creates anything outside schema `health`. Run it
# yourself, once, after creating postgres.env.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DREAMER_PYTHON:-python3}"

"$PY" "$ROOT/scripts/freshness_publish.py" --create-database || exit 1
"$PY" "$ROOT/scripts/freshness_publish.py" --init
"$PY" "$ROOT/scripts/freshness_publish.py" --show
