-- Row level security.
--
-- This is the file that makes tenant isolation real. The developer's own
-- technical approach (§11) named the retrieval/data layer as the top-ranked
-- technical risk on this project, so the rules here are deliberately blunt:
--
--   * RLS is enabled AND forced on every tenant-owned table. Forcing matters --
--     without it the table owner role bypasses every policy below, which is
--     precisely the role a careless server-side client connects as.
--   * Writes are checked with WITH CHECK as well as USING, so a caller cannot
--     insert or re-parent a row into someone else's tenant.
--   * Nothing is granted to anon. Every policy targets authenticated only.
--
-- A cross-tenant read returns zero rows rather than an error, which is what
-- makes the API able to answer 404 and leak nothing (technical approach §6.2).

-- ---------------------------------------------------------------------------
-- Tenants and profiles
-- ---------------------------------------------------------------------------
alter table public.tenants enable row level security;
alter table public.tenants force row level security;

create policy tenants_select_own on public.tenants
    for select to authenticated
    using (id = public.current_tenant_id());

alter table public.profiles enable row level security;
alter table public.profiles force row level security;

-- A user always sees their own row; admins additionally see their tenant's
-- roster so they can manage it.
create policy profiles_select_own_or_tenant_admin on public.profiles
    for select to authenticated
    using (
        user_id = auth.uid()
        or (public.is_tenant_admin() and tenant_id = public.current_tenant_id())
    );

create policy profiles_update_self on public.profiles
    for update to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid() and tenant_id = public.current_tenant_id());

-- ---------------------------------------------------------------------------
-- Brand identity: readable by the whole tenant, writable by its admins only.
-- ---------------------------------------------------------------------------
alter table public.brand_identity enable row level security;
alter table public.brand_identity force row level security;

create policy brand_identity_select on public.brand_identity
    for select to authenticated
    using (tenant_id = public.current_tenant_id());

create policy brand_identity_admin_write on public.brand_identity
    for all to authenticated
    using (tenant_id = public.current_tenant_id() and public.is_tenant_admin())
    with check (tenant_id = public.current_tenant_id() and public.is_tenant_admin());

-- ---------------------------------------------------------------------------
-- Uploads, analyses and outputs: plain tenant scoping.
--
-- Written as a loop because the rule is identical across these tables and
-- spelling it out six times invites one of them to drift.
-- ---------------------------------------------------------------------------
do $$
declare
    target text;
begin
    foreach target in array array[
        'uploads', 'analyses', 'analysis_sections', 'analysis_actions', 'feedback'
    ]
    loop
        execute format('alter table public.%I enable row level security', target);
        execute format('alter table public.%I force row level security', target);

        execute format($f$
            create policy %1$s_tenant_select on public.%1$I
                for select to authenticated
                using (tenant_id = public.current_tenant_id())
        $f$, target);

        execute format($f$
            create policy %1$s_tenant_insert on public.%1$I
                for insert to authenticated
                with check (tenant_id = public.current_tenant_id())
        $f$, target);

        execute format($f$
            create policy %1$s_tenant_update on public.%1$I
                for update to authenticated
                using (tenant_id = public.current_tenant_id())
                with check (tenant_id = public.current_tenant_id())
        $f$, target);

        execute format($f$
            create policy %1$s_tenant_delete on public.%1$I
                for delete to authenticated
                using (tenant_id = public.current_tenant_id())
        $f$, target);
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- Audit log: append-only. Readable within the tenant, insertable, and
-- deliberately given no update or delete policy at all.
-- ---------------------------------------------------------------------------
alter table public.audit_log enable row level security;
alter table public.audit_log force row level security;

create policy audit_log_tenant_select on public.audit_log
    for select to authenticated
    using (tenant_id = public.current_tenant_id());

create policy audit_log_tenant_insert on public.audit_log
    for insert to authenticated
    with check (tenant_id = public.current_tenant_id());

-- Belt and braces. Without a delete policy RLS already reduces a DELETE to
-- zero rows, but it does so silently -- the caller sees a successful statement
-- that removed nothing. Revoking the privilege outright turns a tampering
-- attempt into a loud error that the audit log itself can record.
revoke update, delete on public.audit_log from authenticated;

-- ---------------------------------------------------------------------------
-- Knowledge base: universal content is readable by every authenticated user;
-- tenant-scoped content only by its owner (FR-19, FR-20).
--
-- Ingestion runs as the service role, which bypasses RLS by design, so no
-- write policy is granted to authenticated here.
-- ---------------------------------------------------------------------------
alter table public.kb_documents enable row level security;
alter table public.kb_documents force row level security;

create policy kb_documents_read on public.kb_documents
    for select to authenticated
    using (
        (scope = 'universal')
        or (scope = 'tenant' and tenant_id = public.current_tenant_id())
    );

alter table public.kb_chunks enable row level security;
alter table public.kb_chunks force row level security;

create policy kb_chunks_read on public.kb_chunks
    for select to authenticated
    using (
        (scope = 'universal')
        or (scope = 'tenant' and tenant_id = public.current_tenant_id())
    );

-- ---------------------------------------------------------------------------
-- Storage. Objects live under tenant/{tenant_id}/..., and the first path
-- segment after the prefix is checked against the caller's tenant so the
-- namespace is enforced rather than merely conventional (PRD §8).
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('display-uploads', 'display-uploads', false)
on conflict (id) do nothing;

create policy display_uploads_tenant_read on storage.objects
    for select to authenticated
    using (
        bucket_id = 'display-uploads'
        and (storage.foldername(name))[1] = 'tenant'
        and (storage.foldername(name))[2] = public.current_tenant_id()::text
    );

create policy display_uploads_tenant_write on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'display-uploads'
        and (storage.foldername(name))[1] = 'tenant'
        and (storage.foldername(name))[2] = public.current_tenant_id()::text
    );
