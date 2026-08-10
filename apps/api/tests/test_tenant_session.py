"""Isolation at the session layer, not just in SQL policies.

test_retrieval_cap.py connects as the superuser, so RLS never engages there. This
file exercises the path a real request takes: tenant_session() drops to the
`authenticated` role and sets the caller's identity, and every assertion below
is a query that a leaky implementation would answer.

The last test is the one that matters most for a pooled deployment -- it reuses
the same pool for two different users in sequence and checks the second cannot
see the first's rows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from vmlab.tenancy import session as session_module  # noqa: E402
from vmlab.tenancy.session import (  # noqa: E402
    close_pool,
    resolve_tenant_id,
    service_session,
    tenant_session,
)

CONTAINER = "vmlab-pg-session"
PORT = 55434
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ALPHA = "11111111-1111-1111-1111-111111111111"
BETA = "22222222-2222-2222-2222-222222222222"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker unavailable")


@pytest.fixture(scope="module")
def database() -> str:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", "POSTGRES_PASSWORD=vmlab", "-e", "POSTGRES_DB=vmlab",
            "-p", f"{PORT}:5432", "pgvector/pgvector:pg16",
        ],
        capture_output=True,
        check=True,
    )
    url = f"postgresql://postgres:vmlab@localhost:{PORT}/vmlab"

    for _ in range(60):
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("select 1")
            break
        except Exception:
            time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
        pytest.skip("postgres did not start")

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("create role authenticated nologin")
        for name in [
            "db/test/00_supabase_stubs.sql",
            "db/migrations/0001_extensions_and_tenancy.sql",
            "db/migrations/0002_core_tables.sql",
            "db/migrations/0003_feedback_audit_kb.sql",
            "db/migrations/0004_rls_policies.sql",
        ]:
            with open(os.path.join(ROOT, name)) as handle:
                conn.execute(handle.read())

        conn.execute(
            "insert into public.tenants (id, slug, name) values (%s,'alpha','Alpha'),(%s,'beta','Beta')",
            (ALPHA, BETA),
        )
        conn.execute(
            "insert into auth.users (id, email) values (%s,'a@x.test'),(%s,'b@x.test')",
            (ALICE, BOB),
        )
        conn.execute(
            "insert into public.profiles (user_id, tenant_id, role) values (%s,%s,'admin'),(%s,%s,'user')",
            (ALICE, ALPHA, BOB, BETA),
        )
        for tenant, user, tag in ((ALPHA, ALICE, "alpha"), (BETA, BOB, "beta")):
            conn.execute(
                """
                insert into public.uploads
                    (tenant_id, uploaded_by, storage_path, mime_type, byte_size)
                values (%s, %s, %s, 'image/jpeg', 100)
                """,
                (tenant, user, f"tenant/{tenant}/{tag}.jpg"),
            )
        # The API role must be RLS-subject; granting is normally Supabase's job.
        conn.execute("grant usage on schema public to authenticated")
        conn.execute(
            "grant select, insert, update, delete on all tables in schema public to authenticated"
        )
        conn.execute("revoke update, delete on public.audit_log from authenticated")

    os.environ["DATABASE_URL"] = url
    get_settings = __import__("vmlab.config", fromlist=["get_settings"]).get_settings
    get_settings.cache_clear()
    session_module._pool = None

    yield url

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@pytest.fixture(autouse=True)
async def _reset_pool(database: str):
    yield
    await close_pool()


async def test_session_resolves_the_right_tenant(database: str):
    async with tenant_session(ALICE) as connection:
        assert await resolve_tenant_id(connection) == ALPHA
    async with tenant_session(BOB) as connection:
        assert await resolve_tenant_id(connection) == BETA


async def test_session_sees_only_its_own_uploads(database: str):
    async with tenant_session(BOB) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("select storage_path from public.uploads")
            rows = await cursor.fetchall()

    assert len(rows) == 1
    assert ALPHA not in rows[0][0]


async def test_an_unfiltered_query_cannot_leak(database: str):
    """The forgotten-WHERE-clause case: a bare select must still be scoped."""
    async with tenant_session(BOB) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("select count(*) from public.uploads")
            assert (await cursor.fetchone())[0] == 1

            # Even naming Alpha's row explicitly returns nothing.
            await cursor.execute(
                "select count(*) from public.uploads where tenant_id = %s", (ALPHA,)
            )
            assert (await cursor.fetchone())[0] == 0


async def test_session_runs_as_a_restricted_role(database: str):
    async with tenant_session(BOB) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("select current_user")
            assert (await cursor.fetchone())[0] == "authenticated"


async def test_identity_does_not_leak_across_pooled_requests(database: str):
    """Sequential users on the same pool must not inherit each other's identity."""
    async with tenant_session(ALICE) as connection:
        assert await resolve_tenant_id(connection) == ALPHA

    for _ in range(5):
        async with tenant_session(BOB) as connection:
            assert await resolve_tenant_id(connection) == BETA
            async with connection.cursor() as cursor:
                await cursor.execute("select count(*) from public.uploads")
                assert (await cursor.fetchone())[0] == 1


async def test_service_session_bypasses_rls_for_ingestion(database: str):
    async with service_session() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("select count(*) from public.uploads")
            assert (await cursor.fetchone())[0] == 2
