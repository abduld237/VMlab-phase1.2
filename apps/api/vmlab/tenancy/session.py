"""Per-request database sessions that run under row level security.

The important decision here: the API connects to Postgres as a role that RLS
applies to, and sets the caller's identity on the connection, rather than
connecting as an owner and filtering by tenant in application code.

Those two approaches look equivalent until someone forgets a WHERE clause. Under
this one, a forgotten filter returns no rows; under the other, it returns
everyone's. The developer's own technical approach named the retrieval/data
layer as the top-ranked risk on this project, and the cheapest way to retire
that risk is to make the database the thing enforcing it.

So every query issued during a request runs with:
    set local role authenticated
    set local request.jwt.claim.sub = '<user id>'
which is what public.current_tenant_id() reads. LOCAL scopes both to the
transaction, so a pooled connection cannot leak identity into the next request.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from vmlab.config import get_settings

logger = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = AsyncConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            open=False,
            # Without these a dropped connection is invisible: the pooler goes
            # away, the local socket stays ESTABLISHED forever, and the next
            # query -- or even the pool's own liveness check -- waits on a reply
            # that will never arrive. TCP keepalives make the kernel notice, and
            # tcp_user_timeout bounds how long an unacknowledged send can hang.
            # Keep it well above the slowest legitimate write: at 30s it killed
            # a 1.5MB vector insert mid-flight over a high-latency link, which
            # looks exactly like the dead-socket hang it was added to prevent.
            # Diagnosed from an ingestion stuck 33 minutes with 3.5KB unread on
            # a dead socket while the provider was answering in 131ms.
            kwargs={
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "tcp_user_timeout": 120000,
                "connect_timeout": 15,
            },
            # Reset identity on check-in as a second line of defence. SET LOCAL
            # already dies with the transaction; this covers a connection
            # returned outside one.
            reset=_reset_connection,
            # Validate before handing a connection out. Supabase's pooler closes
            # connections that sit idle, and an analysis leaves them idle for
            # minutes at a time while it waits on model calls. Without this check
            # the pool cheerfully returns a dead socket and the next query waits
            # forever for a reply that will never come -- a real run hung for
            # thirteen minutes that way, and an ingestion for thirty-three.
            check=AsyncConnectionPool.check_connection,
            # Recycle before the pooler gets the chance. Cheaper than
            # discovering the connection is dead on the next acquisition.
            max_idle=120.0,
            max_lifetime=900.0,
        )
        await _pool.open(wait=True, timeout=15)
    return _pool


async def _reset_connection(connection: AsyncConnection) -> None:
    """Clear per-request identity before a connection returns to the pool.

    The commit is required, not tidiness. These statements open an implicit
    transaction, and psycopg discards any connection its reset function hands
    back in INTRANS -- so without committing, every connection is thrown away
    after a single use and the pool silently stops pooling.
    """
    await connection.execute("reset role")
    await connection.execute("select set_config('request.jwt.claim.sub', '', false)")
    await connection.commit()


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@contextlib.asynccontextmanager
async def tenant_session(user_id: str) -> AsyncIterator[AsyncConnection]:
    """A transaction scoped to one authenticated user, subject to RLS.

    Everything inside runs as the `authenticated` role, so tenant boundaries are
    enforced by the database rather than by the caller remembering to filter.
    """
    pool = await open_pool()
    async with pool.connection() as connection:
        async with connection.transaction():
            # Identity first, role second: once the role drops to authenticated
            # it may no longer have permission to set the claim.
            await connection.execute(
                "select set_config('request.jwt.claim.sub', %s, true)", (user_id,)
            )
            await connection.execute("set local role authenticated")
            yield connection


@contextlib.asynccontextmanager
async def service_session() -> AsyncIterator[AsyncConnection]:
    """A privileged session that bypasses RLS.

    For ingestion and administrative tasks only -- never for handling a user
    request. Anything reachable from an HTTP handler must use tenant_session.
    """
    pool = await open_pool()
    async with pool.connection() as connection:
        async with connection.transaction():
            yield connection


async def resolve_tenant_id(connection: AsyncConnection) -> str | None:
    """The active tenant for this session, as the database sees it."""
    async with connection.cursor() as cursor:
        await cursor.execute("select public.current_tenant_id()")
        row = await cursor.fetchone()
    return str(row[0]) if row and row[0] else None
