-- ---------------------------------------------------------------------------
-- Close a privilege escalation in profiles_update_self.
--
-- The original policy let a user update their own profile row and checked only
-- that they could not move themselves to another tenant:
--
--     using (user_id = auth.uid())
--     with check (user_id = auth.uid() and tenant_id = public.current_tenant_id())
--
-- Nothing constrained `role`. A standard user could therefore set their own row
-- to role = 'admin' and gain every admin-gated permission in their tenant --
-- most importantly brand_identity_admin_write, which conditions every analysis
-- the whole workspace runs.
--
-- This was not theoretical and it was not only reachable through our API.
-- Supabase publishes PostgREST at /rest/v1/ against the same database with the
-- same RLS, so any user holding their own session token and the publishable
-- anon key -- both of which are in the browser by design -- could send:
--
--     PATCH /rest/v1/profiles?user_id=eq.<self>   {"role": "admin"}
--
-- Confirmed against the live project: the request returned 200 with role
-- 'admin', after which a brand write that had been refused with 403 seconds
-- earlier succeeded. Our own API had no defect; the database policy was the
-- whole authorisation boundary and it had a hole in it.
--
-- The fix keeps self-service for the one field a user has any business
-- changing, their display name, and freezes tenant and role. Role changes are
-- an administrative act: they belong to an admin acting on someone else's row,
-- never to the subject of the change.
-- ---------------------------------------------------------------------------

drop policy if exists profiles_update_self on public.profiles;

create policy profiles_update_self on public.profiles
    for update to authenticated
    using (user_id = auth.uid())
    with check (
        user_id = auth.uid()
        and tenant_id = public.current_tenant_id()
        -- Neither privilege nor tenancy may be edited by their owner. Compared
        -- against the row as it currently stands rather than against a literal,
        -- so this keeps holding if more roles are added later.
        and role = (select p.role from public.profiles p where p.user_id = auth.uid())
    );

-- An admin may manage the roster of their own tenant, but not their own row --
-- otherwise the escalation returns by another door, since an admin demoting and
-- re-promoting themselves is indistinguishable from a user doing it. Removing
-- the last admin from a tenant is a provisioning concern, handled out of band.
drop policy if exists profiles_admin_manage_roster on public.profiles;

create policy profiles_admin_manage_roster on public.profiles
    for update to authenticated
    using (
        public.is_tenant_admin()
        and tenant_id = public.current_tenant_id()
        and user_id <> auth.uid()
    )
    with check (
        tenant_id = public.current_tenant_id()
        and user_id <> auth.uid()
    );

-- Inserting a profile is provisioning, which is an administrator action taken
-- out of band (see docs/HANDOVER.md). No authenticated user may create one, or
-- they could assign themselves into any tenant they can name.
revoke insert, delete on public.profiles from authenticated;

comment on policy profiles_update_self on public.profiles is
    'A user may edit their own display name only. Role and tenant are frozen: '
    'allowing either would let any user promote themselves to tenant admin.';
