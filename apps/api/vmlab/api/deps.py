"""FastAPI dependencies: authentication and the tenant-scoped database session.

Tenancy is never read from the request. There is no tenant header, no tenant
path parameter and no tenant field in any request body -- the only input is the
bearer token, and the tenant is derived from it server-side. The PRD (§8) asks
for exactly this: "Do not trust a tenant identifier supplied only by the client."
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import AsyncConnection

from vmlab.config import Settings, get_settings
from vmlab.tenancy.session import resolve_tenant_id, tenant_session

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    email: str | None
    tenant_id: str


def settings_dep() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """Cached JWKS client. It caches fetched keys, so build it once."""
    return jwt.PyJWKClient(jwks_url, cache_keys=True)


def _decode(token: str, settings: Settings) -> dict:
    """Verify a Supabase-issued access token.

    Supabase signs tokens two ways depending on when the project was created.
    Older projects use a shared HS256 secret; newer ones default to asymmetric
    signing keys (ES256/RS256) published at a JWKS endpoint, where no shared
    secret exists at all. Both are supported here because which one a project
    uses is not something this code can assume, and getting it wrong presents as
    "invalid token" on every login with no indication why.

    The algorithm is read from the token header only to choose a verification
    path -- it is never trusted to decide *whether* to verify. Signature
    verification is not optional: an unverified decode would let anyone mint a
    token naming any user, and the database would then faithfully enforce
    isolation for whichever identity it was handed.
    """
    try:
        algorithm = jwt.get_unverified_header(token).get("alg", "")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token") from exc

    try:
        if algorithm.startswith(("RS", "ES")):
            if not settings.supabase_url:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "SUPABASE_URL is required to verify asymmetric tokens",
                )
            jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
            signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
            )

        if not settings.supabase_jwt_secret:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "auth is not configured: set SUPABASE_JWT_SECRET, or SUPABASE_URL "
                "if this project uses asymmetric signing keys",
            )
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
    except jwt.PyJWKClientError as exc:
        # Reaching the JWKS endpoint is a server-side problem, not the caller's.
        logger.error("could not fetch JWKS: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "cannot verify tokens right now"
        ) from exc


async def current_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(settings_dep),
) -> AsyncIterator[tuple[CurrentUser, AsyncConnection]]:
    """Authenticate, then open an RLS-scoped session for that user."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    claims = _decode(credentials.credentials, settings)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token has no subject")

    async with tenant_session(user_id) as connection:
        tenant_id = await resolve_tenant_id(connection)
        if tenant_id is None:
            # Authenticated but not assigned to a workspace. Tenants are
            # administrator-created in Phase 1 (FR-16), so this is a
            # provisioning gap rather than something the user can resolve.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "user is not assigned to a workspace"
            )

        yield (
            CurrentUser(user_id=user_id, email=claims.get("email"), tenant_id=tenant_id),
            connection,
        )


async def current_user(
    session: tuple[CurrentUser, AsyncConnection] = Depends(current_session),
) -> CurrentUser:
    return session[0]


async def db(
    session: tuple[CurrentUser, AsyncConnection] = Depends(current_session),
) -> AsyncConnection:
    return session[1]


def not_found() -> HTTPException:
    """The response for anything the caller may not see.

    Deliberately indistinguishable from a genuinely missing record: a 403 on
    another tenant's analysis id would confirm that the id exists, which is a
    membership oracle. RLS already returns zero rows, so handlers reach this by
    finding nothing rather than by running a permission check.
    """
    return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
