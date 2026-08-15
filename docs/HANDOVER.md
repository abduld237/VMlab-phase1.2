# VMlab Phase 1 — Handover

Everything needed to run, operate and continue this system without the original
developer. Written for whoever picks it up next, which may be someone who has
never seen the codebase.

Last updated: 15 August 2026.

---

## 1. Accounts and who owns what

All infrastructure is under **Abdulrazak Mohamed's ownership and billing**, with
the developer as collaborator. Nothing runs on a personal account.

| Service | Purpose | Owner | Where to look |
|---|---|---|---|
| **Supabase** | Postgres, Auth, Storage, pgvector | Abdul | Project ref `iesutfwkzjqwizbmveav`, region `eu-west-2` (London) |
| **OpenRouter** | Vision, reasoning and embedding models | Abdul | One key serves all three |
| **GitHub** | Private repo | Abdul | `abduld237/VMlab-phase1.2`, branch `main` |
| **Railway** | Hosting (not yet deployed) | Abdul | See §8 |

**Credentials live in `.env` at the repo root and are never committed.**
`.gitignore` covers `.env` and `.env.*`. If you need a value you do not have,
regenerate it from the service rather than asking someone to paste it into chat.

The Supabase **database password** is not recoverable — Supabase never displays
it after project creation. It can only be reset, from
`Settings → Database → Reset database password`, which requires **Owner** on the
organisation.

---

## 2. Environment variables

`.env` at the repo root (backend) — copy from `.env.example`:

| Variable | Notes |
|---|---|
| `ENVIRONMENT` | `development` or `production` |
| `DATABASE_URL` | **Must use the session pooler**, see below |
| `SUPABASE_URL` | `https://iesutfwkzjqwizbmveav.supabase.co` |
| `SUPABASE_ANON_KEY` | The publishable key (`sb_publishable_…`) |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side only. Bypasses RLS. Never expose to a browser, never give it a `NEXT_PUBLIC_` name |
| `SUPABASE_JWT_SECRET` | Only for projects still on legacy HS256. This project uses asymmetric keys, so the API verifies via JWKS and this is optional |
| `OPENROUTER_API_KEY` | |

`apps/web/.env.local` (frontend) — Next.js only reads `NEXT_PUBLIC_*` from its
own app directory, never the repo root:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_SUPABASE_URL=https://iesutfwkzjqwizbmveav.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=sb_publishable_…
```

Everything in that file ships to the browser. The service-role key must never
appear in it.

### The connection string is not the one the dashboard shows first

```
postgresql://postgres.iesutfwkzjqwizbmveav:<PASSWORD>@aws-0-eu-west-2.pooler.supabase.com:5432/postgres
```

Two traps. The direct host `db.iesutfwkzjqwizbmveav.supabase.co` is **IPv6-only**
and unreachable from many networks. And the pooler needs the user
`postgres.<project-ref>`, not the bare `postgres` the dashboard string uses.

Percent-encode the password. `,` `+` `@` become `%2C` `%2B` `%40` — un-encoded,
an `@` in the password terminates the userinfo section and the host parses as
nonsense.

---

## 3. Running it locally

Python 3.11+ (built and verified on 3.13.13) and Node 20+.

### One-time setup

From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements-dev.txt
(cd apps/web && npm install)
```

`apps/api/requirements.txt` is the pinned runtime set — the exact versions this
system was verified against. `requirements-dev.txt` adds pytest and ruff on top.
`pyproject.toml` declares the same dependencies as loose ranges and supports
`pip install -e ".[dev]"` if you prefer that; the pinned files are what to use
when you want the build that is known to work.

**Install into the venv, not into a global or conda base environment.** The
`.venv/` directory is gitignored, so it is per-machine and must be recreated
after cloning.

### Every run — two terminals

```bash
# Terminal 1 — backend on :8000
cd apps/api
../../.venv/bin/python -m uvicorn vmlab.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal 2 — frontend on :3000
cd apps/web
npm run dev
```

Open http://localhost:3000/login. The API verifies RLS on all 11 tables at
startup and refuses to serve if any table is unprotected — if startup fails,
read that message before anything else. A healthy boot logs
`row level security verified on 11 tables`.

Start the backend from `apps/api`: the `vmlab` package is imported from the
working directory. Ingestion scripts, by contrast, run from the repo root
because they resolve `data/` relative to it.

If the port is already taken, `uvicorn` still exits non-zero but a stale server
keeps answering on :8000 — and you will spend an hour testing code you did not
just change. Check with `ss -ltnp | grep 8000` before assuming a restart took.

**Tests:**

```bash
cd apps/api && ../../.venv/bin/python -m pytest tests/ -q   # 95 tests
bash db/test/run.sh                                          # SQL isolation, needs Docker
```

`db/test/run.sh` spins up a throwaway Postgres. Do not point it at a real
database: it creates fixture tenants and does not clean up after itself.

---

## 4. Database

Migrations are plain SQL in `db/migrations/`, applied in filename order:

```bash
export PGPASSWORD='<password>'
for f in db/migrations/*.sql; do
  psql -h aws-0-eu-west-2.pooler.supabase.com -p 5432 \
       -U postgres.iesutfwkzjqwizbmveav -d postgres -v ON_ERROR_STOP=1 -f "$f"
done
```

They are idempotent — safe to re-run.

**Do not run `supabase init`.** It scaffolds its own migrations directory, and
two migration systems over one database is how a schema becomes unreadable.
`supabase login` and `supabase link --project-ref iesutfwkzjqwizbmveav` are fine.

### Tenant isolation

Row level security is **enabled and forced** on all 11 tables, keyed off the
JWT's subject via `public.current_tenant_id()`. The API connects as a role RLS
applies to and sets identity per transaction; it does not connect as an owner and
filter in application code. A forgotten `WHERE` clause therefore returns nothing
rather than everyone's data.

A cross-tenant request returns **404, not 403** — a 403 would confirm the id
exists.

`revoke update, delete on public.audit_log from authenticated` is deliberate.
Without it a delete silently succeeds against zero rows and looks like it worked.

---

## 5. Knowledge base

**62 documents, 4,771 chunks**, embedded with `baai/bge-m3` at 1024 dimensions.

The dimension is not arbitrary: pgvector's HNSW index caps at 2000 dimensions.
Changing the embedding model to anything larger means a schema migration and a
full re-embed.

### Re-running ingestion

From the repo root:

```bash
.venv/bin/python scripts/split_bundles.py     # splits the merged PDFs; run first
.venv/bin/python scripts/ingest_kb.py --dry-run
.venv/bin/python scripts/ingest_kb.py
```

Ingestion is idempotent per document — re-running replaces a document's chunks
rather than duplicating them, so an interrupted run can simply be repeated.

### Two things about the source corpus that will confuse you

**The filenames lie.** `VMlab_Document_01_Cover_Page_….pdf` is 371 pages holding
Documents 01–16. `Document.pdf` is 464 pages holding Documents 17–30. Neither is
a single document. `scripts/split_bundles.py` splits them at `DOCUMENT nn`
boundaries; without it every citation carries the wrong document id.

**Documents 11 and 12 each appear twice**, an earlier and a later edition. The
split marks the earlier one superseded and gives it a distinct version suffix
(`1.0-prior1`). This matters because `kb_documents` has
`UNIQUE (document_id, version)` — two editions sharing a version means the second
write deletes the first, and the corpus silently loses the current edition. That
happened once and was only caught by a row count.

Documents 25 and 26 are XLSX workbooks, not PDFs.

---

## 6. How an analysis runs

```
POST /api/uploads     validate, normalise to 1600px, store under tenant/{id}/
POST /api/analyses    creates the row, returns 202 immediately
                      the graph runs detached; status advances through
                      validating → extracting → retrieving → reasoning →
                      synthesising → complete
GET  /api/analyses/{id}   polled by the UI for progress and the result
```

The pipeline is a LangGraph `StateGraph`: one vision call extracts evidence, one
embedding drives three domain-filtered retrievals, three specialists run in
parallel, and synthesis merges them into a ranked top three.

A failing specialist **degrades rather than aborts** — two perspectives plus an
honest disclosure beats an error page. The missing perspective is named in the
uncertainty note.

### Measured, not estimated

All twenty benchmark photographs, run from Dhaka against the live knowledge
base, via `scripts/benchmark_latency.py`:

| | |
|---|---|
| Duration | **p50 18.6s, p90 20.7s, slowest 21.4s** — 20 of 20 inside the 60s target |
| Completion | **20 of 20**, zero retries across 100 model calls |
| Cost | **$0.0066** per analysis, read from OpenRouter's usage accounting |
| Models | vision `google/gemini-2.5-flash-lite`, reasoning `openai/gpt-oss-120b`, embeddings `baai/bge-m3` |

Per stage at p50: evidence 5.4s, retrieval 2.3s, specialists 4.8s (three in
parallel), synthesis 5.0s.

### How it got there, and what to do if it regresses

It was **p50 195s, p90 263s, and 6 of 8 completing** as recently as the same
day. Three things account for the whole difference, and any of them can undo it.

**Provider routing is the big one.** OpenRouter sorts by **price** unless told
otherwise. One model id is not one service: `openai/gpt-oss-120b` is served by
twenty endpoints measured between **18 and 940 tokens/second**, and the three
cheapest — the ones price-sorting always picks — run at 25, 21 and 18. That is
why the same analysis took 119s one run and 360s the next with identical token
counts. `provider.sort = "throughput"` under a price ceiling fixed it;
specialists went from 71s to 5s and synthesis from 41s to 5s. The ceiling
(`provider_max_price_*` in `Settings`) is what keeps cost bounded — remove it
and requests go to Cerebras at ten times the price.

**The vision model must have more than one provider.** `qwen/qwen3.7-flash` was
served by a single endpoint at 24 tok/s, so no routing could help it and it
ignored `reasoning_effort` entirely. It cost 44s per analysis on its own.
`gemini-2.5-flash-lite` does the same work in 6s. Any replacement needs to
support **structured outputs**, or the strict-schema request will find no
eligible provider and fail with a 404 rather than falling back.

**Retries were invisible and expensive.** The vision stage was silently retrying
on four images in six, doubling evidence extraction from ~35s to 73–94s, because
the model answered `signage_legible` with what the sign *said* rather than
true/false. See §7.

If latency regresses, run the harness before changing anything — it reports
which provider served each call, which is almost always the answer.

The Dhaka-to-London round trip is **not** a significant factor, despite what an
earlier version of this document and a comment in `config.py` both claimed:
retrieval is 2.3s of an 18.6s run. Deploying in London is worth a second or two,
not sixty.

### Citations are enforced, not requested

Every `supporting_rule_ids` value is filtered against the rule ids actually
present in the chunks the model was shown. Anything else is dropped.

This exists because the model invented citations that looked exactly right —
`V20-10.1`, `V16-19.3` — none of which exist anywhere in the corpus. A fabricated
citation is worse than none: it reads as authoritative and cannot be checked.

Retrieval reserves 3 of 8 slots for canonical chunks that carry rule ids.
Without that reservation, similarity returns narrative sections and the
specialists have nothing citable — which produced three perspectives and zero
citations on a live run.

---

## 7. Operational traps worth knowing

**Never hold a database connection across a model call.** The graph spends
minutes in OpenRouter; a pooled connection left idle that long is dropped by
Supabase's pooler, and psycopg does not notice — the next query waits forever on
a dead socket. One analysis hung 13 minutes past a 180-second timeout that could
not fire because the coroutine was parked below it. `PerQueryRetriever` acquires
per query for this reason.

The pool now also carries TCP keepalives and `tcp_user_timeout`, so a dropped
connection fails fast instead of hanging. Keep `tcp_user_timeout` comfortably
above the slowest legitimate write — at 30s it aborted a 1.5MB vector insert
mid-flight, which looks identical to the bug it prevents.

**Timeouts are layered and independent.** The httpx read timeout bounds a single
model call (240s); `analysis_timeout_seconds` bounds the whole analysis (420s).
Conflating them wastes a lot of debugging.

**`str(TimeoutError())` is the empty string.** Recording it as an error detail
writes a blank reason and shows the user a failure with no explanation.

**A schema mismatch costs a whole call, so coerce where the answer is right.**
Asked whether signage was legible, the vision model replied `"Magnolia"`,
`"Yes ('THE SHOPPE' on the back wall)"` and `["MAGNOLIA JOURNAL", "ELEVATOR"]` —
every one of them a *more* informative answer to a badly posed question. Strict
validation rejected all three and retried the most expensive call in the
pipeline, on four benchmark images in six. `VisualEvidence` now coerces the
descriptive forms: reading a sign is itself proof it was legible. Prefer that to
a retry whenever the model has clearly understood and merely mis-shaped.

**Watch for escape clauses in prompts — the model will take them.** The
specialist prompt said "produce between 3 and 5 items, fewer is acceptable if
the evidence genuinely does not support more", and a perspective duly came back
with one item. This is the second time this exact pattern has bitten: an earlier
citation instruction ended "if no listed rule supports a point, return an empty
array", and the model returned empty arrays every time. If a floor matters,
state it as a floor.

---

## 8. Deployment (not yet done)

Both services deploy from the GitHub repo. Set the environment variables from §2
in Railway, choose the **London region** so the database round trip stays local,
and point `NEXT_PUBLIC_API_URL` at the deployed API.

After deploying, re-run the §9 verification against the deployed environment.
Passing locally is not the same claim.

---

## 9. Verification checklist

Against the environment you intend to demonstrate:

1. Log in as a tenant-A user, land in tenant A's workspace.
2. Upload a display photo; confirm the object is under `tenant/{A}/…`.
3. Watch the analysis complete; record actual latency.
4. Confirm citations resolve — pick a cited rule id and find it in `kb_chunks`.
5. Confirm three visually distinct perspectives, 3–5 items each, with a reason
   and confidence on every item.
6. Confirm three ranked actions with effort tags, and a trade-off statement
   where specialists disagree.
7. Submit feedback; confirm the row lands against the right analysis and tenant.
8. **Isolation:** authenticate as tenant B and request tenant A's analysis id and
   storage object directly. Both must fail server-side, not merely be hidden.
9. Run the benchmark set and confirm at least 90% complete and p90 inside 60s:

   ```bash
   .venv/bin/python scripts/benchmark_latency.py --limit 20 --label <name>
   .venv/bin/python scripts/benchmark_latency.py --compare final <name>
   ```

   The harness runs the real graph against the live knowledge base without
   writing analysis rows, and reports per-stage timings, the provider that
   served each call, retry counts and cost. Runs are kept in
   `data/benchmark-runs/`, so `--compare` works across days. Change one thing
   at a time and re-measure; bundled changes cannot be attributed.

---

## 10. Known gaps

- **Not deployed.** Everything above runs locally against the live database.
- **Coverage is uneven by domain.** Commercial has a mature bespoke corpus;
  Creative VM and Retail Psychology are thinner, so Commercial output reads
  sharper. That is a property of the source material, not a pipeline fault —
  do not debug it as one.
- **Retail psychology rule ids are sparse** (9 of 2,302 chunks). The domain is
  dominated by one 1,792-chunk third-party handbook with no rule ids at all.
- **Sign-in needs working email.** The login screen sends a magic link, and
  Supabase's built-in mailer allows only a few messages an hour and will not
  deliver to a made-up domain at all. Configure real SMTP in
  `Authentication → Emails` before anyone outside the team tries to sign in.
  A password path exists behind `NEXT_PUBLIC_ALLOW_PASSWORD_LOGIN=true` for
  local testing; leave it unset in production.
- Test users `remon@vmlab.test` (Pilot Retailer One, admin) and
  `tenant-b@vmlab.test` (Pilot Retailer Two, user) exist for isolation testing.
  Delete both before any real pilot.
