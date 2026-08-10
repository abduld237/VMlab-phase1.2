"""Object storage for uploaded display images.

Two backends behind one interface: Supabase Storage in deployment, the local
filesystem for development and tests. The local backend exists so the whole
upload-to-analysis path can be exercised before the client's accounts are live,
not as a production fallback -- `for_settings()` refuses to pick it in production.

Paths are namespaced `tenant/{tenant_id}/{uuid}.jpg` in both backends. The
storage RLS policy checks that second segment against the caller's tenant, so
the layout is what enforces isolation, not a convention this module happens to
follow.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from vmlab.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_path(tenant_id: str, suffix: str = "jpg") -> str:
    return f"tenant/{tenant_id}/{uuid.uuid4()}.{suffix}"


class Storage(ABC):
    @abstractmethod
    async def put(self, path: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def get(self, path: str) -> bytes: ...

    @abstractmethod
    async def signed_url(self, path: str, expires_in: int = 900) -> str: ...


class LocalStorage(Storage):
    """Filesystem-backed storage for development."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        target = (self.root / path).resolve()
        # A path like "tenant/x/../../../etc/passwd" would otherwise escape the
        # root. Storage keys are server-generated today, but this module should
        # not depend on that staying true.
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError("storage path escapes the root directory")
        return target

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def get(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    async def signed_url(self, path: str, expires_in: int = 900) -> str:
        return f"/dev-storage/{path}"


class SupabaseStorage(Storage):
    """Supabase Storage over its REST API.

    Uses the service role key, so this class carries no tenant scoping of its
    own -- callers must only ever hand it a path they derived from the
    authenticated session's tenant.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = f"{settings.supabase_url}/storage/v1"
        self.bucket = settings.storage_bucket
        self.headers = {
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key,
        }

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base}/object/{self.bucket}/{path}",
                content=data,
                headers={**self.headers, "Content-Type": content_type},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"upload failed ({response.status_code}): {response.text[:200]}")

    async def get(self, path: str) -> bytes:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{self.base}/object/{self.bucket}/{path}", headers=self.headers
            )
        if response.status_code >= 400:
            raise RuntimeError(f"download failed ({response.status_code})")
        return response.content

    async def signed_url(self, path: str, expires_in: int = 900) -> str:
        """A short-lived URL. The PRD (§8) requires these be time-limited."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base}/object/sign/{self.bucket}/{path}",
                json={"expiresIn": expires_in},
                headers=self.headers,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"signing failed ({response.status_code})")
        return f"{self.settings.supabase_url}/storage/v1{response.json()['signedURL']}"


def for_settings(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()

    if settings.supabase_url and settings.supabase_service_role_key:
        return SupabaseStorage(settings)

    if settings.is_production:
        # Silently writing customer uploads to a container filesystem that
        # vanishes on redeploy would be worse than refusing to start.
        raise RuntimeError(
            "Supabase storage is not configured, and local storage is not "
            "permitted in production"
        )

    logger.warning("using local filesystem storage -- development only")
    return LocalStorage(Path(".vmlab-storage"))
