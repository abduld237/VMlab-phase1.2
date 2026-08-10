-- Minimal stand-ins for the Supabase-managed schemas, so the migrations can be
-- exercised against plain Postgres in CI and locally.
--
-- Only what the migrations actually touch is stubbed: auth.users, auth.uid(),
-- the storage bucket/object tables and storage.foldername(). Supabase provides
-- richer versions of all of these; the shapes here match closely enough that a
-- policy which passes locally behaves the same way on the real instance.

create schema if not exists auth;
create schema if not exists storage;

create table if not exists auth.users (
    id    uuid primary key default gen_random_uuid(),
    email text unique
);

-- Supabase derives auth.uid() from the request JWT. Locally we drive it from a
-- session GUC so a test can impersonate a user with set_config().
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
    select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;

create table if not exists storage.buckets (
    id     text primary key,
    name   text not null,
    public boolean not null default false
);

create table if not exists storage.objects (
    id        uuid primary key default gen_random_uuid(),
    bucket_id text not null references storage.buckets (id),
    name      text not null,
    owner     uuid
);

alter table storage.objects enable row level security;
alter table storage.objects force row level security;

-- Splits an object key into path segments, as Supabase does.
create or replace function storage.foldername(name text)
returns text[]
language sql
immutable
as $$
    select string_to_array(name, '/');
$$;
