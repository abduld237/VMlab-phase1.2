-- Brand identity, uploads, analyses and their outputs.
--
-- Every tenant-owned table carries tenant_id as its first real column. It is
-- never nullable and never defaulted from the client -- the API layer sets it
-- from current_tenant_id(), and RLS re-checks it on the way in and out.

-- ---------------------------------------------------------------------------
-- Brand identity (FR-17). One row per tenant, managed by that tenant's admin.
-- Conditions analysis for that tenant only; must never leak into another
-- tenant's prompt context (FR-20).
-- ---------------------------------------------------------------------------
create table if not exists public.brand_identity (
    tenant_id       uuid primary key references public.tenants (id) on delete cascade,
    brand_name      text,
    logo_path       text,
    colours         jsonb not null default '[]'::jsonb,
    fonts           jsonb not null default '[]'::jsonb,
    tone_of_voice   text,
    guidelines      text,
    categories      jsonb not null default '[]'::jsonb,
    updated_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Uploads. storage_path is tenant-namespaced (tenant/{tenant_id}/...) and is
-- enforced by the storage policy in 0004, not merely by convention here.
-- ---------------------------------------------------------------------------
create table if not exists public.uploads (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid not null references public.tenants (id) on delete cascade,
    uploaded_by     uuid not null references auth.users (id) on delete restrict,
    storage_path    text not null unique,
    original_name   text,
    mime_type       text not null,
    byte_size       integer not null,
    width           integer,
    height          integer,
    -- Optional context the user may supply before submitting (FR-04).
    display_type    text,
    campaign_objective text,
    hero_product    text,
    -- Validation outcome (FR-05). A borderline image proceeds but carries the
    -- flag through to the final output rather than failing silently.
    quality_flags   jsonb not null default '[]'::jsonb,
    low_confidence  boolean not null default false,
    created_at      timestamptz not null default now()
);

create index if not exists uploads_tenant_created_idx
    on public.uploads (tenant_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Analyses. One row per pipeline run, carrying the observability the PRD asks
-- for in §8: model versions, per-stage latency, token cost.
-- ---------------------------------------------------------------------------
create type public.analysis_status as enum (
    'pending', 'validating', 'extracting', 'retrieving', 'reasoning', 'synthesising',
    'complete', 'failed'
);

create table if not exists public.analyses (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid not null references public.tenants (id) on delete cascade,
    upload_id       uuid not null references public.uploads (id) on delete cascade,
    requested_by    uuid not null references auth.users (id) on delete restrict,
    status          public.analysis_status not null default 'pending',
    -- Doc 25 (VMLAB-EOR-025) shaped observation record from the vision stage.
    visual_evidence jsonb,
    overall_summary text,
    uncertainty_note text,
    -- Which KB chunks were retrieved, so any cross-tenant leak is detectable
    -- after the fact (technical approach §6.2).
    retrieved_chunk_ids uuid[] not null default '{}',
    model_versions  jsonb not null default '{}'::jsonb,
    stage_timings_ms jsonb not null default '{}'::jsonb,
    total_tokens    integer,
    cost_usd        numeric(10, 6),
    error_detail    text,
    created_at      timestamptz not null default now(),
    completed_at    timestamptz
);

create index if not exists analyses_tenant_created_idx
    on public.analyses (tenant_id, created_at desc);

-- ---------------------------------------------------------------------------
-- One section per specialist perspective (FR-07, FR-08, FR-09). Each holds 3-5
-- observation/recommendation items as jsonb, every item carrying its own
-- reason and confidence per FR-11.
-- ---------------------------------------------------------------------------
create type public.perspective as enum ('creative_vm', 'retail_psychology', 'commercial');

create table if not exists public.analysis_sections (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references public.tenants (id) on delete cascade,
    analysis_id  uuid not null references public.analyses (id) on delete cascade,
    perspective  public.perspective not null,
    items        jsonb not null default '[]'::jsonb,
    evidence_refs jsonb not null default '[]'::jsonb,
    created_at   timestamptz not null default now(),
    unique (analysis_id, perspective)
);

create index if not exists analysis_sections_tenant_idx
    on public.analysis_sections (tenant_id);

-- ---------------------------------------------------------------------------
-- The unified top three (FR-10). effort maps to the PRD's required
-- Quick Win / Moderate / Major Change tag; the 0-100 scores and P1-P4 band come
-- from Doc 26 (VMLAB-RDL-026) so the ranking is explainable rather than vibes.
-- ---------------------------------------------------------------------------
create type public.effort_band as enum ('quick_win', 'moderate', 'major_change');

create table if not exists public.analysis_actions (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references public.tenants (id) on delete cascade,
    analysis_id    uuid not null references public.analyses (id) on delete cascade,
    rank           smallint not null check (rank between 1 and 3),
    action         text not null,
    rationale      text not null,
    effort         public.effort_band not null,
    confidence     numeric(3, 2) check (confidence between 0 and 1),
    priority_score smallint check (priority_score between 0 and 100),
    priority_band  text,
    trade_off      text,
    evidence_refs  jsonb not null default '[]'::jsonb,
    created_at     timestamptz not null default now(),
    unique (analysis_id, rank)
);

create index if not exists analysis_actions_tenant_idx
    on public.analysis_actions (tenant_id);
