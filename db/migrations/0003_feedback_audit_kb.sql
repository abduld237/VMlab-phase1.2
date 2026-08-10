-- Feedback capture, the audit log, and the knowledge base.

-- ---------------------------------------------------------------------------
-- Feedback (FR-12). Ratings attach to an individual recommendation rather than
-- the analysis as a whole, which is what makes the monthly review able to say
-- which specialist is weak rather than only that an analysis was unhelpful.
-- ---------------------------------------------------------------------------
create type public.feedback_verdict as enum ('useful', 'partly_useful', 'not_useful');

create table if not exists public.feedback (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references public.tenants (id) on delete cascade,
    analysis_id  uuid not null references public.analyses (id) on delete cascade,
    -- Null target means the rating applies to the analysis overall.
    perspective  public.perspective,
    item_index   smallint,
    action_rank  smallint check (action_rank between 1 and 3),
    verdict      public.feedback_verdict not null,
    correction   text,
    submitted_by uuid not null references auth.users (id) on delete restrict,
    created_at   timestamptz not null default now()
);

create index if not exists feedback_tenant_created_idx
    on public.feedback (tenant_id, created_at desc);
create index if not exists feedback_analysis_idx on public.feedback (analysis_id);

-- ---------------------------------------------------------------------------
-- Audit log (PRD §8 auditability). Append-only: no update or delete policy is
-- granted in 0004, so a compromised session cannot erase its own trail.
-- ---------------------------------------------------------------------------
create table if not exists public.audit_log (
    id            bigserial primary key,
    tenant_id     uuid references public.tenants (id) on delete set null,
    actor_id      uuid references auth.users (id) on delete set null,
    action        text not null,
    resource_type text,
    resource_id   text,
    model_version text,
    detail        jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);

create index if not exists audit_log_tenant_created_idx
    on public.audit_log (tenant_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Knowledge base.
--
-- The corpus is the client's own and is universal in scope (FR-19), so chunks
-- are shared rather than tenant-owned. The scope and tenant_id columns exist
-- anyway: tenant-private knowledge is a Phase 2 certainty, and retrofitting a
-- scope boundary onto a live index is exactly the kind of change that leaks.
-- A null tenant_id means universal.
-- ---------------------------------------------------------------------------
create type public.kb_scope as enum ('universal', 'tenant');
create type public.kb_domain as enum ('creative_vm', 'retail_psychology', 'commercial', 'system');
create type public.kb_authority as enum ('canonical', 'guidance', 'illustrative', 'historical');
create type public.kb_validity as enum ('active', 'superseded', 'deprecated', 'retired');

create table if not exists public.kb_documents (
    id             uuid primary key default gen_random_uuid(),
    document_id    text not null,
    title          text not null,
    domain         public.kb_domain not null,
    scope          public.kb_scope not null default 'universal',
    tenant_id      uuid references public.tenants (id) on delete cascade,
    authority      public.kb_authority not null default 'canonical',
    validity_status public.kb_validity not null default 'active',
    version        text not null default '1.0',
    rule_prefixes  text[] not null default '{}',
    source_path    text,
    page_count     integer,
    ingested_at    timestamptz not null default now(),
    unique (document_id, version),
    -- Universal knowledge must not carry a tenant, and tenant knowledge must.
    constraint kb_documents_scope_tenant_ck check (
        (scope = 'universal' and tenant_id is null)
        or (scope = 'tenant' and tenant_id is not null)
    )
);

-- Embeddings come from baai/bge-m3 via OpenRouter's /v1/embeddings endpoint:
-- open-weight, $0.01/M tokens, and 1024 dimensions.
--
-- The dimension is a hard constraint, not a preference. pgvector's HNSW index
-- tops out at 2000 dimensions, which rules out Qwen3 Embedding 8B (4096 native)
-- and 4B (2560) unless their output is Matryoshka-truncated first. bge-m3 fits
-- natively, so nothing downstream has to compensate. Changing this value means
-- re-embedding the whole corpus, so it belongs in a migration rather than config.
create table if not exists public.kb_chunks (
    id              uuid primary key default gen_random_uuid(),
    kb_document_id  uuid not null references public.kb_documents (id) on delete cascade,
    domain          public.kb_domain not null,
    scope           public.kb_scope not null default 'universal',
    tenant_id       uuid references public.tenants (id) on delete cascade,
    authority       public.kb_authority not null default 'canonical',
    validity_status public.kb_validity not null default 'active',
    chunk_index     integer not null,
    content         text not null,
    content_type    text not null default 'text',
    section_path    text,
    page            integer,
    rule_ids        text[] not null default '{}',
    retrieval_tags  text[] not null default '{}',
    embedding       vector(1024),
    created_at      timestamptz not null default now(),
    unique (kb_document_id, chunk_index),
    constraint kb_chunks_scope_tenant_ck check (
        (scope = 'universal' and tenant_id is null)
        or (scope = 'tenant' and tenant_id is not null)
    )
);

-- Retrieval always filters by domain and validity before ranking by distance
-- (Doc 29 §13: authority over similarity), so those columns lead the index.
create index if not exists kb_chunks_domain_validity_idx
    on public.kb_chunks (domain, validity_status, authority);
create index if not exists kb_chunks_rule_ids_idx on public.kb_chunks using gin (rule_ids);

create index if not exists kb_chunks_embedding_idx
    on public.kb_chunks using hnsw (embedding vector_cosine_ops);
