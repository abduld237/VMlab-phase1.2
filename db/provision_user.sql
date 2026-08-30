-- Assign a signed-in user to a workspace (FR-16).
--
-- Provisioning is manual by design: `authenticated` has no insert on profiles,
-- so anyone who signs in before this is run gets 403 "user is not assigned to a
-- workspace" from deps.py. That is correct behaviour and it is also the first
-- thing every new tester hits, because signing up and being provisioned are
-- separate steps and nothing in the product joins them up. Run this when you
-- invite someone, not when they report the error.
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -v email=them@example.com -v tenant=pilot-two -v role=admin \
--        -v name='Their Name' -f db/provision_user.sql
--
-- Roles are 'admin' or 'user'. Admin may rewrite the workspace brand profile,
-- which conditions every analysis the whole tenant runs -- so it is not a
-- courtesy title. Tenant slugs: pilot-one, pilot-two, pilot-three.
--
-- Idempotent. Re-running for someone who already has a profile changes nothing
-- and reports the row they already have; moving a user between workspaces is a
-- deliberate update, not a re-run, because it orphans them from everything they
-- have already analysed.

-- The checks are meta-commands rather than a DO block on purpose: psql does not
-- interpolate variables inside dollar-quoted strings, so a plpgsql guard would
-- silently compare the literal text :'email' and always pass.
select
    exists (select 1 from auth.users where email = :'email') as have_user,
    exists (select 1 from public.tenants where slug = :'tenant') as have_tenant
\gset

\if :have_user
\else
\echo 'ABORT: no auth user' :'email' '-- they must sign in once first.'
\quit
\endif

\if :have_tenant
\else
\echo 'ABORT: no tenant with slug' :'tenant' '-- try pilot-one, -two, -three.'
\quit
\endif

insert into public.profiles (user_id, tenant_id, role, display_name)
select u.id, t.id, :'role'::user_role, :'name'
from auth.users u, public.tenants t
where u.email = :'email' and t.slug = :'tenant'
on conflict (user_id) do nothing;

select u.email, t.name as workspace, t.slug, p.role, p.display_name, p.created_at
from public.profiles p
join auth.users u on u.id = p.user_id
join public.tenants t on t.id = p.tenant_id
where u.email = :'email';
