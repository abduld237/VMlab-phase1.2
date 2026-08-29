# VMlab Phase 1

A retail user photographs a display; the system returns three specialist reviews
— Creative/Visual Merchandising, Retail Psychology and Commercial — plus one
prioritised set of top-three actions, grounded in a curated knowledge base.

Month 1 milestone: 1–31 August 2026.

## Layout

```
apps/api/        FastAPI backend, LangGraph pipeline, retrieval, ingestion
apps/web/        Next.js frontend (mobile-first)
db/migrations/   Schema, RLS policies, pgvector
db/test/         Migration runner and the tenant isolation suite
scripts/         Corpus splitting, ingestion, model preflight, latency benchmark
docs/            Handover notes
data/benchmark/  20 real display photographs, the acceptance set
```

## Running it

Prerequisites: Python 3.11+ (built on 3.13) and Node 20+. Docker only for the
database-backed tests.

### 1. Configuration, once

```bash
cp .env.example .env          # then fill it in
```

`docs/HANDOVER.md` §2 has the values and two traps worth reading before you
guess: the database host is **not** the one Supabase shows you first, and the
password must be percent-encoded. `apps/web/.env.local` is separate — Next.js
reads `NEXT_PUBLIC_*` only from its own directory, never from the repo root.

### 2. Install, once

From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements-dev.txt   # runtime + pytest + ruff
(cd apps/web && npm install)
```

Use the venv rather than whatever Python is on `PATH`. `requirements.txt` pins
the versions this system was verified against; `pyproject.toml` declares the
same set as loose ranges for tooling. `.venv/` is gitignored, so it has to be
recreated after cloning.

### 3. Run — two terminals

```bash
# Terminal 1 — API on :8000
cd apps/api && ../../.venv/bin/python -m uvicorn vmlab.main:app --port 8000 --reload

# Terminal 2 — web on :3000
cd apps/web && npm run dev
```

Open <http://localhost:3000/login>.

A healthy API logs `row level security verified on 11 tables`. If that line is
missing the API refuses to serve at all — read the startup error before
anything else, because it means a table is unprotected.

Start the API from `apps/api`: the `vmlab` package is imported from the working
directory. Ingestion and benchmark scripts run from the **repo root** instead,
because they resolve `data/` relative to it.

### 4. Stopping

```bash
pkill -f "uvicorn vmlab.main:app"
pkill -f "next dev"; pkill -f next-server
```

Confirm with `ss -ltnp | grep -E ':8000|:3000'` — it should print nothing. A
stale server on :8000 keeps answering after a failed restart, and you will
spend an hour testing code you did not just change.

### Signing in

Login is a magic link, and Supabase's built-in mailer sends only a few messages
an hour and will not deliver to a made-up domain at all. For local work set

```
NEXT_PUBLIC_ALLOW_PASSWORD_LOGIN=true
```

in `apps/web/.env.local` and restart the frontend. Leave it unset in production
and configure real SMTP under `Authentication → Emails`.

**A signed-in user still needs a workspace.** Tenants are administrator-created
(FR-16) and `authenticated` cannot insert into `profiles`, so a new account gets
`403 user is not assigned to a workspace` until someone assigns it:

```sql
insert into public.profiles (user_id, tenant_id, role, display_name)
select u.id, t.id, 'admin', 'Their Name'
from auth.users u, public.tenants t
where u.email = 'them@example.com' and t.name = 'Pilot Retailer Three';
```

That 403 is the isolation model working, not a bug. See `docs/HANDOVER.md` §11.

## Tests

```bash
cd apps/api && ../../.venv/bin/python -m pytest -q    # 99 tests
cd apps/web && npm run typecheck && npx next build
bash db/test/run.sh                                   # SQL-level isolation assertions
```

Docker is required for the database-backed tests; they skip without it.
`db/test/run.sh` spins up a throwaway Postgres, creates fixture tenants and does
not clean up — never point it at a real database.

**Run `db/test/run.sh` before every deploy.** It drives SQL directly rather than
going through the API, which is the only way to test an authorisation boundary
honestly: our API is not the only door to this database. Supabase publishes
PostgREST over the same tables under the same RLS, so a policy that is loose
enough to matter is reachable with credentials the browser already holds. A
privilege escalation that let any user promote themselves to tenant admin lived
here for weeks and was invisible to every API-level test.

## Knowledge base

The client's corpus arrived as two merged PDFs holding ~30 canonical documents
with misleading filenames. Splitting them is a prerequisite for everything else
— without it, `document_id` is wrong on 800+ pages and every rule citation is
untrustworthy.

```bash
.venv/bin/python scripts/split_bundles.py       # 29 documents -> data/split/
.venv/bin/python scripts/check_models.py        # verify configured model IDs
.venv/bin/python scripts/ingest_kb.py --dry-run # chunk without embedding or writing
.venv/bin/python scripts/ingest_kb.py           # embed and write; idempotent per document
```

The live corpus is **62 documents, 4,771 chunks**. Re-run the dry run after any
change to chunking and compare the count — a swing means the rule-block handling
has broken, which is far cheaper to catch there than in retrieval results.

## Measuring latency

```bash
.venv/bin/python scripts/benchmark_latency.py --limit 20 --label mychange
.venv/bin/python scripts/benchmark_latency.py --compare final mychange
```

Runs the real pipeline over `data/benchmark/` without writing analysis rows, and
records per-stage timings, **which provider served each call**, retry counts and
cost to `data/benchmark-runs/`. Change one thing at a time: bundled changes
cannot be attributed, and provider choice alone moved p50 from 195s to 50s.

## Design decisions worth knowing

**Tenant isolation is enforced by Postgres, not application code.** The API
connects as a role that RLS applies to and sets the caller's identity per
request. A forgotten `WHERE` clause returns nothing rather than everything.
Cross-tenant requests return 404, not 403, since 403 would confirm the id exists.

**Retrieval ranks authority before similarity.** The corpus's own Document 29
states that the most semantically similar text is not necessarily the governing
rule. Superseded editions are excluded outright rather than ranked down, and
illustrative sources are capped at 25% of any bundle — one third-party handbook
is 78% of the retail psychology domain and would otherwise crowd out the
client's own standards.

**The evidence stage may only emit `Observed only`.** Enforced by a validator.
Once a judgement is recorded as an observation, every downstream specialist
treats it as fact and the reasoning becomes uncheckable.

**A failing specialist degrades rather than aborting** — and the result says so.
A two-perspective analysis that looks complete is worse than an error.

## Status

All seven Month-1 acceptance criteria pass end to end against the live Supabase
project. Measured over all twenty benchmark photographs: **p50 18.6s, p90 20.7s,
20 of 20 inside the PRD's 60-second target**, at $0.0066 per analysis. It was
p50 195s before the latency work; `docs/HANDOVER.md` §6 explains what moved and
what would undo it.

Deployed on Railway — frontend at
https://ravishing-courage-production-7828.up.railway.app, API at
https://vmlab-phase12-production.up.railway.app. Configuration for both
services lives in `apps/api/railway.json` and `apps/web/railway.json`, with the
setup steps and traps in `docs/HANDOVER.md` §8. Sign-in needs real SMTP
configured before anyone outside the team can log in, so no analysis has yet
been run through the deployed frontend.
