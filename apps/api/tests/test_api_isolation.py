"""The API surface, exercised over HTTP against a real database.

The isolation tests elsewhere prove the SQL policies and the session layer. This
file proves the thing a penetration tester would actually try: authenticate as a
real user of tenant B, then request tenant A's resources by id over HTTP and
confirm the API returns 404 rather than data -- and 404 rather than 403, because
403 would confirm the id exists.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import uuid

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
jwt = pytest.importorskip("jwt")
httpx = pytest.importorskip("httpx")

CONTAINER = "vmlab-pg-api"
PORT = 55435
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SECRET = "test-jwt-secret-not-a-real-one"

ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOBODY = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ALPHA = "11111111-1111-1111-1111-111111111111"
BETA = "22222222-2222-2222-2222-222222222222"
ALPHA_ANALYSIS = "a0000000-0000-0000-0000-0000000000a1"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker unavailable")


def token_for(user_id: str) -> str:
    return jwt.encode(
        {"sub": user_id, "aud": "authenticated", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )


def photo_bytes() -> bytes:
    image = Image.new("RGB", (1200, 900), (140, 140, 140))
    draw = ImageDraw.Draw(image)
    for i in range(0, 1200, 21):
        draw.line([(i, 0), (i, 900)], fill=(230, 40, 90), width=3)
    for i in range(0, 900, 17):
        draw.line([(0, i), (1200, i)], fill=(20, 200, 140), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def api(tmp_path_factory):
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
            "insert into public.tenants (id,slug,name) values (%s,'alpha','Alpha'),(%s,'beta','Beta')",
            (ALPHA, BETA),
        )
        conn.execute(
            "insert into auth.users (id,email) values (%s,'a@x.test'),(%s,'b@x.test'),(%s,'c@x.test')",
            (ALICE, BOB, NOBODY),
        )
        conn.execute(
            "insert into public.profiles (user_id,tenant_id,role) values (%s,%s,'admin'),(%s,%s,'user')",
            (ALICE, ALPHA, BOB, BETA),
        )
        conn.execute(
            "insert into public.brand_identity (tenant_id,brand_name) values (%s,'Alpha Secret Brand')",
            (ALPHA,),
        )
        upload_id = uuid.uuid4()
        conn.execute(
            """insert into public.uploads (id,tenant_id,uploaded_by,storage_path,mime_type,byte_size)
               values (%s,%s,%s,%s,'image/jpeg',10)""",
            (upload_id, ALPHA, ALICE, f"tenant/{ALPHA}/secret.jpg"),
        )
        conn.execute(
            """insert into public.analyses (id,tenant_id,upload_id,requested_by,status,overall_summary)
               values (%s,%s,%s,%s,'complete','Alpha confidential summary')""",
            (ALPHA_ANALYSIS, ALPHA, upload_id, ALICE),
        )
        conn.execute("grant usage on schema public to authenticated")
        conn.execute(
            "grant select,insert,update,delete on all tables in schema public to authenticated"
        )
        conn.execute("revoke update,delete on public.audit_log from authenticated")
        conn.execute("grant usage,select on all sequences in schema public to authenticated")

    os.environ.update(
        DATABASE_URL=url,
        SUPABASE_JWT_SECRET=SECRET,
        ENVIRONMENT="development",
        SUPABASE_URL="",
        SUPABASE_SERVICE_ROLE_KEY="",
    )
    os.chdir(tmp_path_factory.mktemp("storage"))

    from vmlab.config import get_settings
    from vmlab.tenancy import session as session_module

    get_settings.cache_clear()
    session_module._pool = None

    from vmlab.main import app

    yield app
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@pytest.fixture
async def client(api):
    """A client per test, with the connection pool torn down afterwards.

    pytest-asyncio gives each test its own event loop, and an AsyncConnectionPool
    is bound to the loop that opened it. Carrying one across tests deadlocks on
    the second acquire rather than failing, so the pool is closed between tests
    and rebuilt lazily on first use.
    """
    from vmlab.tenancy.session import close_pool

    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await close_pool()


def auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {token_for(user_id)}"}


# -- authentication ---------------------------------------------------------


async def test_requests_without_a_token_are_rejected(client):
    assert (await client.get("/api/analyses")).status_code == 401


async def test_a_forged_token_is_rejected(client):
    forged = jwt.encode(
        {"sub": ALICE, "aud": "authenticated", "exp": int(time.time()) + 3600},
        "the-wrong-secret",
        algorithm="HS256",
    )
    response = await client.get("/api/analyses", headers={"Authorization": f"Bearer {forged}"})

    # Signature verification is the whole basis of tenant identity: a token
    # signed with any key would let a caller name any user.
    assert response.status_code == 401


async def test_an_expired_token_is_rejected(client):
    expired = jwt.encode(
        {"sub": ALICE, "aud": "authenticated", "exp": int(time.time()) - 10},
        SECRET,
        algorithm="HS256",
    )
    response = await client.get("/api/analyses", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


async def test_a_user_with_no_workspace_is_refused(client):
    response = await client.get("/api/analyses", headers=auth(NOBODY))
    assert response.status_code == 403


# -- cross-tenant access ----------------------------------------------------


async def test_another_tenants_analysis_returns_404_not_403(client):
    response = await client.get(f"/api/analyses/{ALPHA_ANALYSIS}", headers=auth(BOB))

    assert response.status_code == 404, "403 would confirm the id exists"
    assert "Alpha confidential" not in response.text


async def test_the_owner_can_read_their_own_analysis(client):
    response = await client.get(f"/api/analyses/{ALPHA_ANALYSIS}", headers=auth(ALICE))

    assert response.status_code == 200
    assert response.json()["overall_summary"] == "Alpha confidential summary"


async def test_listing_shows_only_the_callers_workspace(client):
    alpha = (await client.get("/api/analyses", headers=auth(ALICE))).json()
    beta = (await client.get("/api/analyses", headers=auth(BOB))).json()

    assert len(alpha) == 1
    assert beta == []


async def test_brand_profile_does_not_cross_tenants(client):
    alpha = (await client.get("/api/brand", headers=auth(ALICE))).json()
    beta = (await client.get("/api/brand", headers=auth(BOB))).json()

    assert alpha["brand_name"] == "Alpha Secret Brand"
    assert beta == {}


async def test_feedback_on_another_tenants_analysis_is_refused(client):
    response = await client.post(
        f"/api/analyses/{ALPHA_ANALYSIS}/feedback",
        json={"verdict": "useful"},
        headers=auth(BOB),
    )
    assert response.status_code == 404


# -- upload -----------------------------------------------------------------


async def test_upload_accepts_a_valid_photo(client):
    response = await client.post(
        "/api/uploads",
        files={"file": ("display.jpg", photo_bytes(), "image/jpeg")},
        data={"display_type": "end-cap"},
        headers=auth(BOB),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["low_confidence"] is False
    assert max(body["width"], body["height"]) <= 1600


async def test_upload_rejects_a_non_image(client):
    response = await client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"definitely not an image", "text/plain")},
        headers=auth(BOB),
    )

    assert response.status_code == 422
    assert "readable image" in response.text


async def test_an_uploaded_image_lands_under_its_own_tenant_prefix(client, api):
    await client.post(
        "/api/uploads",
        files={"file": ("display.jpg", photo_bytes(), "image/jpeg")},
        headers=auth(BOB),
    )

    from vmlab.tenancy.session import service_session

    async with service_session() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "select storage_path from public.uploads where tenant_id = %s", (BETA,)
            )
            paths = [row[0] for row in await cursor.fetchall()]

    assert paths
    assert all(path.startswith(f"tenant/{BETA}/") for path in paths)


# -- starting an analysis ---------------------------------------------------


async def test_starting_an_analysis_on_another_tenants_upload_is_refused(client):
    """The upload id is the only thing the caller supplies, so it is the attack.

    RLS makes Bob's upload invisible to Alice, so the lookup finds nothing and
    the handler returns 404 -- the analysis must never be created, and no work
    must be paid for on another tenant's image.
    """
    created = await client.post(
        "/api/uploads",
        files={"file": ("display.jpg", photo_bytes(), "image/jpeg")},
        headers=auth(BOB),
    )
    upload_id = created.json()["upload_id"]

    response = await client.post(
        "/api/analyses", json={"upload_id": upload_id}, headers=auth(ALICE)
    )

    assert response.status_code == 404, response.text

    from vmlab.tenancy.session import service_session

    async with service_session() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "select count(*) from public.analyses where upload_id = %s", (upload_id,)
            )
            assert (await cursor.fetchone())[0] == 0, "no row may be created for a refused request"


async def test_starting_an_analysis_on_a_missing_upload_is_refused(client):
    response = await client.post(
        "/api/analyses", json={"upload_id": str(uuid.uuid4())}, headers=auth(BOB)
    )
    assert response.status_code == 404


async def test_starting_an_analysis_requires_authentication(client):
    response = await client.post("/api/analyses", json={"upload_id": str(uuid.uuid4())})
    assert response.status_code in (401, 403)


# -- brand profile writes ---------------------------------------------------


async def test_brand_updates_do_not_cross_tenants(client):
    """Bob's save must never touch Alice's brand profile.

    The update is scoped by the session's tenant, and RLS forces it, so there is
    no request field that could redirect the write.
    """
    before = await client.get("/api/brand", headers=auth(ALICE))
    alice_name = before.json().get("brand_name")

    await client.put(
        "/api/brand",
        json={"brand_name": "Bob's Rebrand", "colours": [], "fonts": [], "categories": []},
        headers=auth(BOB),
    )

    after = await client.get("/api/brand", headers=auth(ALICE))
    assert after.json().get("brand_name") == alice_name, "another tenant's brand changed"


async def test_brand_update_requires_authentication(client):
    response = await client.put("/api/brand", json={"brand_name": "Anon"})
    assert response.status_code in (401, 403)
