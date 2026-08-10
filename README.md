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
scripts/         Corpus splitting, ingestion, model preflight
docs/            Open dependencies and handover notes
```

## Running it

```bash
# 1. Database (Docker) — applies migrations and runs the isolation suite
./db/test/run.sh

# 2. Backend
cd apps/api && pip install -e ".[dev]"
uvicorn vmlab.main:app --reload          # http://localhost:8000/docs

# 3. Frontend
cd apps/web && npm install && npm run dev
```

Copy `.env.example` to `.env` first. The API refuses to start if row level
security is not enabled and forced on every tenant-owned table.

## Tests

```bash
cd apps/api && pytest              # 40 tests
cd apps/web && npm run typecheck && npx next build
./db/test/run.sh                   # SQL-level isolation assertions
```

Docker is required for the database-backed tests; they skip without it.

## Knowledge base

The client's corpus arrived as two merged PDFs holding ~30 canonical documents
with misleading filenames. Splitting them is a prerequisite for everything else
— without it, `document_id` is wrong on 800+ pages and every rule citation is
untrustworthy.

```bash
python3 scripts/split_bundles.py       # 29 documents -> data/split/
python3 scripts/check_models.py        # verify configured model IDs
python3 scripts/ingest_kb.py --dry-run # chunk without embedding or writing
```

The dry run currently produces 4,533 chunks from 59 sources at an estimated
$0.019 to embed. Run it after any change to chunking and compare — a swing in
chunk count means the rule-block handling has broken, which is far cheaper to
catch there than in retrieval results.

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

See `docs/OPEN_DEPENDENCIES.md`. In short: the pipeline cannot run live until an
OpenRouter key exists, and the acceptance demo needs real display photographs,
which the client's corpus does not contain.
