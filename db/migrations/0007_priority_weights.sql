-- Record the priority mix each analysis was run with.
--
-- The three specialists are weighted per analysis from the sliders on the
-- capture screen (creative_vm / retail_psychology / commercial, summing to
-- 100). The mix changes how many findings each perspective produces, how fully
-- it writes them, and which perspective keeps a point when two of them make the
-- same one -- so without it stored, a past analysis cannot be explained and two
-- runs over the same photograph look inexplicably different.
--
-- No RLS change: the column rides the existing policies on public.analyses.
--
-- Rows written before this migration keep '{}', which the UI reads as
-- "balanced, not recorded" rather than as three zeroes. Defaulting them to
-- 34/33/33 would be a guess written into history as a fact -- and it would
-- claim a mix the user never chose.

alter table public.analyses
    add column if not exists priority_weights jsonb not null default '{}'::jsonb;

comment on column public.analyses.priority_weights is
    'Specialist priority mix for this run, keyed by perspective, summing to 100. '
    'Empty for analyses run before the mix existed.';
