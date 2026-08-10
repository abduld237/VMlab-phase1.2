"""Retrieval behaviour against a real Postgres, not a mock.

The scenario is the corpus's actual shape: retail psychology where a bulky
third-party handbook outnumbers the client's canonical documents roughly 4:1,
and -- crucially -- is also *closer* to the query. That combination is what
defeats authority ranking on its own, so it is what the cap has to survive.

Requires Docker. Skipped if unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

psycopg = pytest.importorskip("psycopg")

from vmlab.retrieval.retriever import Retriever  # noqa: E402

CONTAINER = "vmlab-pg-pytest"
DIMS = 1024
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker unavailable")


def _vector(value: float) -> list[float]:
    """A unit-ish vector whose first component carries the signal.

    Cosine distance then varies only with `value`, which makes "how close is
    this chunk to the query" directly controllable in the fixtures.
    """
    vec = [0.0] * DIMS
    vec[0] = 1.0
    vec[1] = value
    return vec


@pytest.fixture(scope="module")
def dsn() -> str:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", "POSTGRES_PASSWORD=vmlab", "-e", "POSTGRES_DB=vmlab",
            "-p", "55433:5432", "pgvector/pgvector:pg16",
        ],
        capture_output=True,
        check=True,
    )
    url = "postgresql://postgres:vmlab@localhost:55433/vmlab"

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
        ]:
            with open(os.path.join(ROOT, name)) as handle:
                conn.execute(handle.read())

    yield url
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@pytest.fixture(scope="module")
def seeded(dsn: str) -> str:
    """Seed the imbalance: 40 illustrative chunks, all nearer than 10 canonical."""
    canonical_doc = uuid.uuid4()
    illustrative_doc = uuid.uuid4()

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            insert into public.kb_documents
                (id, document_id, title, domain, scope, authority, validity_status)
            values
                (%s, 'VMLAB-KB-14A.1', 'Foundations of Consumer Psychology',
                 'retail_psychology', 'universal', 'canonical', 'active'),
                (%s, 'THIRD-PARTY-HANDBOOK', 'Handbook of Consumer Psychology',
                 'retail_psychology', 'universal', 'illustrative', 'active')
            """,
            (canonical_doc, illustrative_doc),
        )

        rows = []
        # Illustrative chunks sit closest to the query, mimicking a bulky source
        # that wins on similarity alone.
        for i in range(40):
            rows.append((illustrative_doc, "illustrative", i, f"handbook passage {i}", _vector(0.01 * i)))
        # Canonical chunks are all further away.
        for i in range(10):
            rows.append((canonical_doc, "canonical", i, f"canonical rule {i}", _vector(5.0 + i)))

        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into public.kb_chunks
                    (kb_document_id, domain, scope, authority, validity_status,
                     chunk_index, content, embedding)
                values (%s, 'retail_psychology', 'universal', %s, 'active', %s, %s, %s::vector)
                """,
                [(d, a, i, c, str(v)) for d, a, i, c, v in rows],
            )

        # One superseded canonical chunk, closer than everything else. It must
        # never appear -- GRA-089.
        conn.execute(
            """
            insert into public.kb_documents
                (id, document_id, title, domain, scope, authority, validity_status)
            values (%s, 'VMLAB-KB-OLD', 'Superseded Edition',
                    'retail_psychology', 'universal', 'canonical', 'superseded')
            """,
            (old_doc := uuid.uuid4(),),
        )
        conn.execute(
            """
            insert into public.kb_chunks
                (kb_document_id, domain, scope, authority, validity_status,
                 chunk_index, content, embedding)
            values (%s, 'retail_psychology', 'universal', 'canonical', 'superseded',
                    0, 'superseded rule', %s::vector)
            """,
            (old_doc, str(_vector(0.0))),
        )
    return dsn


async def _search(dsn: str, top_k: int = 8):
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        return await Retriever(conn).search(
            domain="retail_psychology",
            embedding=_vector(0.0),
            tenant_id=None,
            top_k=top_k,
        )


async def test_illustrative_share_is_capped(seeded: str):
    chunks = await _search(seeded, top_k=8)

    assert len(chunks) == 8
    illustrative = [c for c in chunks if c.authority == "illustrative"]
    # 25% of 8 -- without the cap all 8 would be illustrative, since those are
    # the 40 nearest vectors in the table.
    assert len(illustrative) <= 2, f"cap breached: {len(illustrative)} illustrative"


async def test_canonical_survives_a_closer_bulky_source(seeded: str):
    chunks = await _search(seeded, top_k=8)

    canonical = [c for c in chunks if c.authority == "canonical"]
    assert canonical, "canonical material was crowded out entirely"
    # Authority outranks distance, so canonical leads despite being further away.
    assert chunks[0].authority == "canonical"


async def test_superseded_chunks_never_returned(seeded: str):
    chunks = await _search(seeded, top_k=20)

    assert all(c.document_id != "VMLAB-KB-OLD" for c in chunks)
    assert all("superseded" not in c.content for c in chunks)


async def test_citations_are_traceable(seeded: str):
    chunks = await _search(seeded, top_k=4)

    for chunk in chunks:
        assert chunk.document_id
        assert chunk.citation.startswith(chunk.document_id)
