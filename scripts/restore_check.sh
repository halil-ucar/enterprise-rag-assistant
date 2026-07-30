#!/usr/bin/env bash
# restore_check.sh (A5): prove RESTORABILITY, not just backup existence.
#
# Dump the live database, restore it into a throwaway database, compare row
# counts of the tables that matter, drop the throwaway. Any count mismatch
# (or any failing step) exits non-zero.
#
# Uses the standard PG environment (PGHOST/PGUSER/PGPASSWORD/PGDATABASE) and
# runs as rag_owner — superuser in the postgres container AND in the CI
# service, so CREATE EXTENSION vector inside the restore is uncritical and
# the rag_app/rag_audit_reader grants restore cleanly (roles are
# cluster-wide).
#
# Honest counts: whatever the current data state is (CI: test residue, owner
# machine: seeded corpus) gets compared 1:1 — low or zero counts still prove
# the MECHANISM; meaningful numbers come from a run after seeding.
set -euo pipefail

SOURCE_DB="${PGDATABASE:-rag}"
CHECK_DB="rag_restore_check"
TABLES="documents chunks feedback audit_events"

count() {
    psql -tA -d "$1" -c "SELECT count(*) FROM $2"
}

declare -A SOURCE_COUNTS
for t in $TABLES; do
    SOURCE_COUNTS[$t]="$(count "$SOURCE_DB" "$t")"
done

DUMP="$(mktemp /tmp/rag_restore_check_XXXXXX.dump)"
trap 'rm -f "$DUMP"' EXIT

pg_dump -Fc -d "$SOURCE_DB" -f "$DUMP"

psql -d "$SOURCE_DB" -q -c "DROP DATABASE IF EXISTS $CHECK_DB"
psql -d "$SOURCE_DB" -q -c "CREATE DATABASE $CHECK_DB"
pg_restore -d "$CHECK_DB" "$DUMP"

STATUS=0
SUMMARY=""
for t in $TABLES; do
    RESTORED="$(count "$CHECK_DB" "$t")"
    if [ "$RESTORED" != "${SOURCE_COUNTS[$t]}" ]; then
        echo "restore-check MISMATCH: $t source=${SOURCE_COUNTS[$t]} restored=$RESTORED" >&2
        STATUS=1
    fi
    SUMMARY="$SUMMARY $t=${SOURCE_COUNTS[$t]}"
done

psql -d "$SOURCE_DB" -q -c "DROP DATABASE $CHECK_DB"

if [ "$STATUS" -ne 0 ]; then
    exit "$STATUS"
fi
echo "restore-check OK:$SUMMARY"
