"""VMlab API entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vmlab.api.routes import analyses
from vmlab.config import get_settings
from vmlab.tenancy.session import close_pool, open_pool, service_session

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    await assert_rls_enabled()
    yield
    await close_pool()


async def assert_rls_enabled() -> None:
    """Refuse to start if any tenant-owned table is missing forced RLS.

    A migration that creates a table and forgets its policies is silent: the
    application keeps working, and every tenant can read every row. Checking at
    boot turns that into a failed deploy instead of a data leak nobody notices.
    """
    expected = {
        "tenants", "profiles", "brand_identity", "uploads", "analyses",
        "analysis_sections", "analysis_actions", "feedback", "audit_log",
        "kb_documents", "kb_chunks",
    }

    async with service_session() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                select relname, relrowsecurity, relforcerowsecurity
                from pg_class
                where relnamespace = 'public'::regnamespace
                  and relkind = 'r'
                  and relname = any(%s)
                """,
                (list(expected),),
            )
            rows = await cursor.fetchall()

    seen = {name for name, _, _ in rows}
    missing = expected - seen
    unprotected = [
        name for name, enabled, forced in rows if not (enabled and forced)
    ]

    if missing:
        raise RuntimeError(f"tables absent -- run migrations: {sorted(missing)}")
    if unprotected:
        raise RuntimeError(
            f"row level security is not enabled and forced on: {sorted(unprotected)}"
        )
    logger.info("row level security verified on %d tables", len(seen))


settings = get_settings()

app = FastAPI(
    title="VMlab API",
    version="0.1.0",
    description="Retail display analysis — Phase 1 prototype",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(analyses.router)


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


@app.get("/health/db", tags=["ops"])
async def health_db() -> dict[str, object]:
    async with service_session() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("select count(*) from public.kb_chunks")
            chunks = (await cursor.fetchone())[0]
            await cursor.execute("select count(*) from public.tenants")
            tenants = (await cursor.fetchone())[0]
    return {"status": "ok", "tenants": tenants, "kb_chunks": chunks}
