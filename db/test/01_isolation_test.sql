-- Tenant isolation test.
--
-- Proves the claim the PRD's §11.3 acceptance check actually cares about: that
-- a user of one tenant cannot reach another tenant's data by asking for it
-- directly, by id, with a valid session. Every assertion here is a read or
-- write that MUST fail or return nothing.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Seed: two tenants, one user each, one upload and one analysis each.
-- ---------------------------------------------------------------------------
insert into public.tenants (id, slug, name) values
    ('11111111-1111-1111-1111-111111111111', 'alpha', 'Alpha Retail'),
    ('22222222-2222-2222-2222-222222222222', 'beta',  'Beta Stores');

insert into auth.users (id, email) values
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'alice@alpha.test'),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bob@beta.test');

insert into public.profiles (user_id, tenant_id, role, display_name) values
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'admin', 'Alice'),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '22222222-2222-2222-2222-222222222222', 'user',  'Bob');

insert into public.uploads (id, tenant_id, uploaded_by, storage_path, mime_type, byte_size) values
    ('a0000000-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'tenant/11111111-1111-1111-1111-111111111111/a.jpg', 'image/jpeg', 1234),
    ('b0000000-0000-0000-0000-000000000001', '22222222-2222-2222-2222-222222222222',
     'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'tenant/22222222-2222-2222-2222-222222222222/b.jpg', 'image/jpeg', 1234);

insert into public.analyses (id, tenant_id, upload_id, requested_by, status) values
    ('a0000000-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111',
     'a0000000-0000-0000-0000-000000000001', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'complete'),
    ('b0000000-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222',
     'b0000000-0000-0000-0000-000000000001', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'complete');

insert into public.brand_identity (tenant_id, brand_name) values
    ('11111111-1111-1111-1111-111111111111', 'Alpha Secret Brand'),
    ('22222222-2222-2222-2222-222222222222', 'Beta Secret Brand');

-- Universal knowledge plus one tenant-private chunk owned by Alpha.
insert into public.kb_documents (id, document_id, title, domain, scope, tenant_id) values
    ('c0000000-0000-0000-0000-000000000001', 'VMLAB-KB-12', 'Product Presentation', 'creative_vm', 'universal', null),
    ('c0000000-0000-0000-0000-000000000002', 'ALPHA-PRIV-01', 'Alpha Private Guide', 'creative_vm', 'tenant',
     '11111111-1111-1111-1111-111111111111');

insert into public.kb_chunks (kb_document_id, domain, scope, tenant_id, chunk_index, content) values
    ('c0000000-0000-0000-0000-000000000001', 'creative_vm', 'universal', null, 0, 'Universal VM guidance'),
    ('c0000000-0000-0000-0000-000000000002', 'creative_vm', 'tenant',
     '11111111-1111-1111-1111-111111111111', 0, 'Alpha private merchandising rule');

grant usage on schema public, auth, storage to authenticated;
grant select, insert, update, delete on all tables in schema public to authenticated;
grant select, insert on storage.objects to authenticated;
grant usage, select on all sequences in schema public to authenticated;

-- The blanket grant above is deliberately over-broad so the policies, not the
-- grants, are what the assertions exercise. Re-apply the one revoke the
-- migration makes, which the blanket grant has just undone.
revoke update, delete on public.audit_log from authenticated;

-- ---------------------------------------------------------------------------
-- Become Bob (tenant Beta) and try to reach Alpha's data.
-- ---------------------------------------------------------------------------
set role authenticated;
select set_config('request.jwt.claim.sub', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', false);

do $$
declare
    found integer;
begin
    if public.current_tenant_id() <> '22222222-2222-2222-2222-222222222222' then
        raise exception 'FAIL: current_tenant_id() resolved to %', public.current_tenant_id();
    end if;

    select count(*) into found from public.uploads
        where id = 'a0000000-0000-0000-0000-000000000001';
    if found <> 0 then raise exception 'FAIL: read % Alpha uploads by id', found; end if;

    select count(*) into found from public.analyses
        where id = 'a0000000-0000-0000-0000-000000000002';
    if found <> 0 then raise exception 'FAIL: read % Alpha analyses by id', found; end if;

    select count(*) into found from public.brand_identity
        where brand_name = 'Alpha Secret Brand';
    if found <> 0 then raise exception 'FAIL: read Alpha brand identity'; end if;

    select count(*) into found from public.uploads;
    if found <> 1 then raise exception 'FAIL: unfiltered uploads returned % rows', found; end if;

    select count(*) into found from public.tenants;
    if found <> 1 then raise exception 'FAIL: unfiltered tenants returned % rows', found; end if;

    select count(*) into found from public.kb_chunks where scope = 'universal';
    if found <> 1 then raise exception 'FAIL: universal chunks returned % rows', found; end if;

    select count(*) into found from public.kb_chunks
        where content = 'Alpha private merchandising rule';
    if found <> 0 then raise exception 'FAIL: read Alpha private KB chunk'; end if;

    raise notice 'PASS: cross-tenant reads all blocked';
end
$$;

-- Writing into Alpha's tenant must be rejected outright, not silently accepted.
do $$
begin
    begin
        insert into public.uploads (tenant_id, uploaded_by, storage_path, mime_type, byte_size)
        values ('11111111-1111-1111-1111-111111111111',
                'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                'tenant/11111111-1111-1111-1111-111111111111/evil.jpg', 'image/jpeg', 1);
        raise exception 'FAIL: inserted an upload into Alpha tenant';
    exception
        when insufficient_privilege then
            raise notice 'PASS: cross-tenant insert rejected';
    end;
end
$$;

-- Re-parenting one of Bob's own rows into Alpha must also be rejected.
do $$
declare
    moved integer;
begin
    begin
        update public.uploads
           set tenant_id = '11111111-1111-1111-1111-111111111111'
         where id = 'b0000000-0000-0000-0000-000000000001';
        get diagnostics moved = row_count;
        raise exception 'FAIL: re-parented % row(s) into Alpha', moved;
    exception
        when insufficient_privilege then
            raise notice 'PASS: cross-tenant re-parent rejected';
    end;
end
$$;

-- Storage: Bob must not see or write objects under Alpha's prefix.
do $$
declare
    found integer;
begin
    begin
        insert into storage.objects (bucket_id, name)
        values ('display-uploads', 'tenant/11111111-1111-1111-1111-111111111111/steal.jpg');
        raise exception 'FAIL: wrote an object under Alpha prefix';
    exception
        when insufficient_privilege then
            raise notice 'PASS: cross-tenant storage write rejected';
    end;

    select count(*) into found from storage.objects
        where name like 'tenant/11111111-1111-1111-1111-111111111111/%';
    if found <> 0 then raise exception 'FAIL: read % Alpha storage objects', found; end if;
    raise notice 'PASS: cross-tenant storage read blocked';
end
$$;

-- The audit log must be append-only even within one's own tenant.
do $$
begin
    insert into public.audit_log (tenant_id, actor_id, action)
    values ('22222222-2222-2222-2222-222222222222',
            'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'test.event');

    begin
        delete from public.audit_log where action = 'test.event';
        raise exception 'FAIL: deleted an audit log row';
    exception
        when insufficient_privilege then
            raise notice 'PASS: audit log delete rejected';
    end;
end
$$;

reset role;
\echo 'ALL ISOLATION ASSERTIONS PASSED'
