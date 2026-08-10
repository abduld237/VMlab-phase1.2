-- The three pilot retailer workspaces (FR-16, PRD §6.3: at least three).
--
-- Placeholder names until the pilot retailers are confirmed -- the client was
-- asked on 16 July whether the three are signed up and has not answered. Renaming
-- a tenant is a one-line update; the ids are what everything else references,
-- so they are fixed here rather than generated.
--
-- Idempotent so it is safe to re-run against an existing database.

insert into public.tenants (id, slug, name) values
    ('00000000-0000-4000-8000-000000000001', 'pilot-one',   'Pilot Retailer One'),
    ('00000000-0000-4000-8000-000000000002', 'pilot-two',   'Pilot Retailer Two'),
    ('00000000-0000-4000-8000-000000000003', 'pilot-three', 'Pilot Retailer Three')
on conflict (id) do nothing;

insert into public.brand_identity (tenant_id, brand_name) values
    ('00000000-0000-4000-8000-000000000001', 'Pilot Retailer One'),
    ('00000000-0000-4000-8000-000000000002', 'Pilot Retailer Two'),
    ('00000000-0000-4000-8000-000000000003', 'Pilot Retailer Three')
on conflict (tenant_id) do nothing;
