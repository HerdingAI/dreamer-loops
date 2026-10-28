#!/usr/bin/env bash
# One-time setup for the freshness display.
# Creates the database named by PGDATABASE on the Postgres server described in
# ~/.config/dreamer/postgres.env (override: DREAMER_PG_ENV) if it does not
# exist, then publishes one snapshot and reads it back. Publishing creates
# schema `health`, and schema `board` when the owner enabled it.
#
# This is the only step that creates anything outside schema `health`. Run it
# yourself, once, after creating postgres.env.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DREAMER_PYTHON:-python3}"

"$PY" "$ROOT/scripts/freshness_publish.py" --create-database || exit 1
# Schema `board` (content). Prints "disabled" and creates nothing unless
# DREAMER_PUBLISH_BOARD=1 is set in postgres.env.
"$PY" "$ROOT/scripts/board_publish.py" --init
"$PY" "$ROOT/scripts/freshness_publish.py" --init
"$PY" "$ROOT/scripts/freshness_publish.py" --show
