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

# Share of each result set reserved for canonical chunks that actually carry
# rule ids. Enough that a specialist has something citable; small enough that
# similarity still decides most of what it reads.
RULE_BEARING_FRACTION = 0.375


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
        ) as tier_rank,
        row_number() over (
            partition by (cardinality(c.rule_ids) > 0)
            order by c.embedding <=> %(embedding)s::vector
        ) as rule_rank
    from public.kb_chunks c
    join public.kb_documents d on d.id = c.kb_document_id
    where c.domain = %(domain)s
      and c.validity_status = 'active'
      and c.embedding is not null
      and (
          c.scope = 'universal'
          or (c.scope = 'tenant' and c.tenant_id = %(tenant_id)s)
      )
),
-- Fewer than a fifth of canonical chunks carry rule ids: the standards split
-- between narrative sections and the rule tables that codify them, and the
-- narrative reads as more similar to a photograph's description. Similarity
-- therefore returns eight canonical chunks with nothing citable, which is
-- exactly what produced three perspectives and zero citations on a live run.
-- Reserving a few slots for the nearest rule-bearing chunks keeps the client's
-- own rule ids reachable without displacing the best matches wholesale.
reserved as (
    select * from candidates
    where cardinality(rule_ids) > 0
      and authority = 'canonical'
      and rule_rank <= %(rule_quota)s
),
general as (
    select *,
        row_number() over (
            order by
                case authority
                    when 'canonical' then 0
                    when 'guidance' then 1
                    when 'illustrative' then 2
                    else 3
                end,
                distance
        ) as general_rank
    from candidates
    where (authority <> 'illustrative' or tier_rank <= %(illustrative_cap)s)
      and id not in (select id from reserved)
)
-- The reserved rows have to be taken out of the budget before the rest compete,
-- not merged and re-sorted with them. Sorting the union by distance and then
-- applying the limit let a whole domain's rule-bearing chunks fall off the end
-- whenever they sat further away than eight narrative ones -- which is why
-- retail psychology kept coming back with nothing citable.
select * from (
    select id, document_id, title, domain, authority, content, section_path,
           page, rule_ids, distance
    from reserved
    union all
    select id, document_id, title, domain, authority, content, section_path,
           page, rule_ids, distance
    from general
    where general_rank <= %(limit)s - (select count(*) from reserved)
) merged
order by
    case authority
        when 'canonical' then 0
        when 'guidance' then 1
        when 'illustrative' then 2
        else 3
    end,
    distance
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
        rule_quota = max(1, int(top_k * RULE_BEARING_FRACTION))

        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                _SEARCH_SQL,
                {
                    "embedding": embedding,
                    "domain": domain,
                    "tenant_id": tenant_id,
                    "illustrative_cap": illustrative_cap,
                    "rule_quota": rule_quota,
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
