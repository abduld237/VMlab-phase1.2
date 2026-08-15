"""Embed chunks and write them to the knowledge base.

Runs under the service role, bypassing RLS, because the corpus is universal
knowledge shared across every tenant rather than anything a tenant owns.

Two things this handles that a naive loop would not. Embedding is batched, since
one request per chunk would mean 4,500 round trips for a corpus that fits in
about seventy. And ingestion is idempotent per document: re-running replaces a
document's chunks in a single transaction rather than duplicating them, so a run
interrupted halfway can simply be repeated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg import AsyncConnection

from vmlab.ingestion.chunking import Chunk
from vmlab.models.openrouter import OpenRouterClient

logger = logging.getLogger(__name__)

# Large enough that the corpus needs ~70 requests rather than thousands, small
# enough to stay well inside the model's 8k context across a batch.
EMBED_BATCH = 48

# Rows per insert statement. Keeps each round trip to roughly 200KB of vector
# payload, which a slow link can acknowledge comfortably.
INSERT_BATCH = 10


@dataclass
class DocumentRecord:
    document_id: str
    title: str
    domain: str
    authority: str
    validity_status: str
    version: str
    rule_prefixes: list[str]
    source_path: str
    page_count: int | None = None


async def embed_chunks(
    client: OpenRouterClient, chunks: list[Chunk], *, batch_size: int = EMBED_BATCH
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors.extend(await client.embed([chunk.content for chunk in batch]))
    return vectors


async def write_document(
    connection: AsyncConnection,
    document: DocumentRecord,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> str:
    """Insert or replace one document and its chunks. Returns the row id."""
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"{len(chunks)} chunks but {len(embeddings)} embeddings for "
            f"{document.document_id} -- refusing to write misaligned vectors"
        )

    async with connection.cursor() as cursor:
        # Re-running ingestion must not duplicate. The cascade clears the old
        # chunks, and doing it inside the caller's transaction means a failure
        # partway leaves the previous version intact rather than a half-written
        # document.
        #
        # validity_status is part of the key, not just the id and version: a
        # superseded edition and the active one that replaced it are different
        # documents that legitimately share both. Keying on two columns made the
        # second write delete the first, and the corpus silently lost Document
        # 11's current 60-page edition to its 29-page predecessor.
        await cursor.execute(
            """
            delete from public.kb_documents
            where document_id = %s and version = %s and validity_status = %s
            """,
            (document.document_id, document.version, document.validity_status),
        )
        await cursor.execute(
            """
            insert into public.kb_documents
                (document_id, title, domain, scope, authority, validity_status,
                 version, rule_prefixes, source_path, page_count)
            values (%s,%s,%s,'universal',%s,%s,%s,%s,%s,%s)
            returning id
            """,
            (
                document.document_id, document.title, document.domain,
                document.authority, document.validity_status, document.version,
                document.rule_prefixes, document.source_path, document.page_count,
            ),
        )
        document_row_id = (await cursor.fetchone())[0]

        rows = [
            (
                document_row_id, document.domain, document.authority,
                document.validity_status, chunk.chunk_index, chunk.content,
                chunk.content_type, chunk.section_path or None, chunk.page,
                chunk.rule_ids, str(vector),
            )
            for chunk, vector in zip(chunks, embeddings, strict=True)
        ]

        # Sent in batches rather than one statement. A 1024-dimension vector
        # serialises to roughly 20KB, so a large document is well over a
        # megabyte in a single executemany -- enough to sit unacknowledged long
        # enough on a high-latency link for the kernel to abort the connection
        # mid-write. Smaller statements also mean a stall shows up as a slow
        # write rather than a dead socket.
        for start in range(0, len(rows), INSERT_BATCH):
            await cursor.executemany(
                """
                insert into public.kb_chunks
                    (kb_document_id, domain, scope, authority, validity_status,
                     chunk_index, content, content_type, section_path, page,
                     rule_ids, embedding)
                values (%s,%s,'universal',%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                """,
                rows[start : start + INSERT_BATCH],
            )

    return str(document_row_id)
