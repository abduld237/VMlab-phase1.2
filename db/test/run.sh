#!/usr/bin/env bash
# Apply every migration to a throwaway Postgres and run the isolation assertions.
#
# Runs against plain Postgres with pgvector plus the Supabase stubs in
# 00_supabase_stubs.sql, so it needs no cloud project and is safe to run in CI
# or on a laptop. The isolation test is the PRD §11.3 acceptance check in
# executable form -- it should be run before every deploy, not just once.

set -euo pipefail

CONTAINER=vmlab-pg-test
IMAGE=pgvector/pgvector:pg16
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "starting $IMAGE..."
docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=vmlab -e POSTGRES_DB=vmlab \
    "$IMAGE" >/dev/null

# pg_isready is not sufficient here: the official image brings the server up on
# a local socket during initdb and then restarts it, so a readiness check can
# pass against an instance that is about to disappear. Probe with a real query
# instead, which only succeeds once the final server is accepting connections.
ready=false
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" psql -U postgres -d vmlab -q -c 'select 1' >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done

if [[ "$ready" != true ]]; then
    echo "postgres did not become ready" >&2
    docker logs "$CONTAINER" 2>&1 | tail -20 >&2
    exit 1
fi

psql_run() {
    docker exec -i "$CONTAINER" psql -U postgres -d vmlab -v ON_ERROR_STOP=1 -q
}

# Supabase provisions this role; plain Postgres does not.
docker exec "$CONTAINER" psql -U postgres -d vmlab -q \
    -c "create role authenticated nologin;" >/dev/null

psql_run < "$ROOT/db/test/00_supabase_stubs.sql"
for migration in "$ROOT"/db/migrations/*.sql; do
    printf '  applying %s\n' "$(basename "$migration")"
    psql_run < "$migration"
done

echo
echo "running isolation assertions..."
if docker exec -i "$CONTAINER" psql -U postgres -d vmlab -v ON_ERROR_STOP=1 \
        < "$ROOT/db/test/01_isolation_test.sql" 2>&1 \
        | grep -E 'NOTICE|ERROR|PASSED' | sed 's/^psql://'; then
    echo
    echo "isolation test passed"
else
    echo
    echo "isolation test FAILED" >&2
    exit 1
fi
