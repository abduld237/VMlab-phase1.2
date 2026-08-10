-- Extensions, tenant model, and the helper functions every RLS policy leans on.
--
-- The client's PRD (§8) is explicit that tenant identity must be resolved
-- server-side and that a tenant identifier supplied by the client must never be
-- trusted. So tenancy is derived from the authenticated user's profile row via
-- current_tenant_id(), not from a request header, body field or client-set JWT
-- claim. There is no code path that lets a caller name its own tenant.

create extension if not exists "vector";
create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Tenants: one retailer workspace each. Administrator-created only; Phase 1
-- has no self-service provisioning (PRD §6.3).
-- ---------------------------------------------------------------------------
create table if not exists public.tenants (
    id          uuid primary key default gen_random_uuid(),
    slug        text not null unique,
    name        text not null,
    created_at  timestamptz not null default now()
);

comment on table public.tenants is
    'One retailer workspace. Admin-created; no self-service onboarding in Phase 1.';

-- ---------------------------------------------------------------------------
-- Profiles: the join between a Supabase auth user and its tenant.
--
-- A user belongs to exactly one tenant in Phase 1. The unique constraint on
-- user_id is what makes current_tenant_id() single-valued and therefore safe to
-- inline into policies.
-- ---------------------------------------------------------------------------
create type public.user_role as enum ('admin', 'user');

create table if not exists public.profiles (
    user_id     uuid primary key references auth.users (id) on delete cascade,
    tenant_id   uuid not null references public.tenants (id) on delete restrict,
    role        public.user_role not null default 'user',
    display_name text,
    created_at  timestamptz not null default now()
);

create index if not exists profiles_tenant_idx on public.profiles (tenant_id);

comment on table public.profiles is
    'Maps an authenticated user to exactly one tenant. Source of truth for tenant resolution.';

-- ---------------------------------------------------------------------------
-- Tenant resolution helpers.
--
-- security definer so they can read profiles regardless of the caller's own RLS
-- context -- without this, the policy on profiles would recurse into itself.
-- search_path is pinned so a caller cannot shadow public with their own schema.
-- ---------------------------------------------------------------------------
create or replace function public.current_tenant_id()
returns uuid
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select tenant_id from public.profiles where user_id = auth.uid();
$$;

create or replace function public.is_tenant_admin()
returns boolean
language sql
stable
security definer
set search_path = public, pg_catalog
as $$
    select coalesce(
        (select role = 'admin' from public.profiles where user_id = auth.uid()),
        false
    );
$$;

comment on function public.current_tenant_id() is
    'The calling user''s tenant, resolved server-side from profiles. Never client-supplied.';

revoke execute on function public.current_tenant_id() from public;
revoke execute on function public.is_tenant_admin() from public;
grant execute on function public.current_tenant_id() to authenticated;
grant execute on function public.is_tenant_admin() to authenticated;
