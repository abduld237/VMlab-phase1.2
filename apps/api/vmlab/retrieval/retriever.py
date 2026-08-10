"""Knowledge retrieval: domain-filtered, authority-first, with a volume cap.

Document 29 §13 states the doctrine this implements: "The most semantically
similar text is not necessarily the governing rule." So similarity is the last
sort key, not the first. Ordering is authority, then validity, then distance.

The cap exists because of a volume problem in the corpus. Retail psychology
holds 2,302 chunks, and 1,792 of them come from one third-party handbook -- 78%
of the domain from a single non-canonical source. Authority ranking alone does
not fix that: with enough chunks, a bulky illustrative source wins on sheer
probability of having something close to any given query, and can fill a bundle
before canonical material gets a look in. Capping the illustrative share
guarantees the client's own standards are represented in every result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# Lower rank sorts first. Superseded content is excluded outright rather than
# ranked down -- GRA-089 requires that it never outrank active canonical content,
# and the split produced two superseded editions that would otherwise contradict
# their replacements.
AUTHORITY_RANK = {"canonical": 0, "guidance": 1, "illustrative": 2, "historical": 3}

# At most this share of a bundle may come from illustrative sources.
MAX_ILLUSTRATIVE_FRACTION = 0.25


@dataclass
class RetrievedChunk:
    id: str
    document_id: str
    title: str
    domain: str
    authority: str
    content: str
    section_path: str | None
    page: int | None
    rule_ids: list[str]
    distance: float

    @property
    def citation(self) -> str:
        """A human-readable source reference for the UI's evidence display."""
        parts = [self.document_id]
        if self.section_path:
            parts.append(f"§{self.section_path}")
        if self.page:
            parts.append(f"p{self.page}")
        return " ".join(parts)


# Ranks by authority first and distance last, and numbers rows within each
# authority tier so the caller can apply a per-tier cap without a second query.
_SEARCH_SQL = """
with candidates as (
    select
        c.id,
        d.document_id,
        d.title,
        c.domain::text          as domain,
        c.authority::text       as authority,
        c.content,
        c.section_path,
        c.page,
        c.rule_ids,
        c.embedding <=> %(embedding)s::vector as distance,
        row_number() over (
            partition by c.authority
            order by c.embedding <=> %(embedding)s::vector
        ) as tier_rank
    from public.kb_chunks c
    join public.kb_documents d on d.id = c.kb_document_id
    where c.domain = %(domain)s
      and c.validity_status = 'active'
      and c.embedding is not null
      and (
          c.scope = 'universal'
          or (c.scope = 'tenant' and c.tenant_id = %(tenant_id)s)
      )
)
select *
from candidates
where authority <> 'illustrative' or tier_rank <= %(illustrative_cap)s
order by
    case authority
        when 'canonical' then 0
        when 'guidance' then 1
        when 'illustrative' then 2
        else 3
    end,
    distance
limit %(limit)s
"""


class Retriever:
    def __init__(self, connection: AsyncConnection):
        self.connection = connection

    async def search(
        self,
        *,
        domain: str,
        embedding: list[float],
        tenant_id: str | None,
        top_k: int = 8,
    ) -> list[RetrievedChunk]:
        """Retrieve for one specialist domain.

        tenant_id scopes any tenant-private knowledge; universal chunks are
        always eligible. Passing None restricts the search to universal content.
        """
        illustrative_cap = max(1, int(top_k * MAX_ILLUSTRATIVE_FRACTION))

        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                _SEARCH_SQL,
                {
                    "embedding": embedding,
                    "domain": domain,
                    "tenant_id": tenant_id,
                    "illustrative_cap": illustrative_cap,
                    "limit": top_k,
                },
            )
            rows = await cursor.fetchall()

        chunks = [
            RetrievedChunk(
                id=str(row["id"]),
                document_id=row["document_id"],
                title=row["title"],
                domain=row["domain"],
                authority=row["authority"],
                content=row["content"],
                section_path=row["section_path"],
                page=row["page"],
                rule_ids=list(row["rule_ids"] or []),
                distance=float(row["distance"]),
            )
            for row in rows
        ]

        canonical = sum(1 for c in chunks if c.authority == "canonical")
        if chunks and canonical == 0:
            # Not an error -- some domains genuinely have no canonical coverage
            # yet -- but it is worth surfacing, because an answer built entirely
            # from third-party material should not be presented as grounded in
            # the client's standards.
            logger.warning(
                "retrieval for domain=%s returned %d chunks with no canonical source",
                domain,
                len(chunks),
            )
        return chunks
