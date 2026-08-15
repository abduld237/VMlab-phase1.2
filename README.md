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

Copy `.env.example` to `.env` and fill it in first (see `docs/HANDOVER.md` §2 —
the connection string is not the one Supabase shows you first). The API refuses
to start if row level security is not enabled and forced on every tenant-owned
table.

**One-time setup**, from the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements-dev.txt   # runtime + pytest + ruff
(cd apps/web && npm install)
```

Use the venv rather than whatever Python happens to be on `PATH`. The pins in
`apps/api/requirements.txt` are the versions this system was built and verified
against; `pyproject.toml` declares the same set as loose ranges for tooling.

**Every run** — two terminals:

```bash
# Terminal 1 — backend on :8000
cd apps/api && ../../.venv/bin/python -m uvicorn vmlab.main:app --port 8000 --reload

# Terminal 2 — frontend on :3000
cd apps/web && npm run dev
```

Then open <http://localhost:3000/login>. `apps/web/.env.local` holds the
frontend's own variables — Next.js only reads `NEXT_PUBLIC_*` from the app
directory, never from the repo root `.env`.

Running the backend from `apps/api` matters: the `vmlab` package is imported
from the working directory, and ingestion scripts resolve `data/` relative to
the repo root.

## Tests

```bash
cd apps/api && ../../.venv/bin/python -m pytest -q    # 95 tests
cd apps/web && npm run typecheck && npx next build
./db/test/run.sh                                      # SQL-level isolation assertions
```

Docker is required for the database-backed tests; they skip without it.
`db/test/run.sh` creates fixture tenants and does not clean up — never point it
at a real database.

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

Not yet deployed, and sign-in needs real SMTP configured before anyone outside
the team can log in.
