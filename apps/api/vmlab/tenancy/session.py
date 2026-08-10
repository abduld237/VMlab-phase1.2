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
            # Reset identity on check-in as a second line of defence. SET LOCAL
            # already dies with the transaction; this covers a connection
            # returned outside one.
            reset=_reset_connection,
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
