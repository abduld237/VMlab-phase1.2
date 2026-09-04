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
cd apps/api && ../../.venv/bin/python -m pytest tests/ -q   # 99 tests
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
parallel, a reconcile pass removes findings two of them both made, and synthesis
merges what is left into a ranked top three.

A failing specialist **degrades rather than aborts** — two perspectives plus an
honest disclosure beats an error page. The missing perspective is named in the
uncertainty note.

`build_graph` is called inside `run_analysis`, so every analysis compiles its own
graph. There is no cached or long-lived graph object anywhere, and nothing to
invalidate when a setting changes.

### The priority mix

Three sliders on the capture screen weight the specialists for one analysis.
They total 100, floor at 10 and ceiling at 80 (the ceiling follows from the
floor: the other two cannot go below 10 between them). The mix is stored on
`analyses.priority_weights`, because two runs over the same photograph differ
when the mix differs and an unexplainable report is not worth much.

**The floor is load-bearing.** Zero is always reachable with three sliders and a
fixed total, and zero would silence a perspective FR-07/08/09 require every
analysis to carry. Ten percent is quiet, not absent.

A weight becomes a depth band in `graph/priorities.py` — one place, shared by the
API's request validation and the specialist node:

| weight | items | what changes |
|---|---|---|
| ≤20 | 3 | headline points only, one-sentence reasons |
| 21–45 | 3–4 | **the pre-slider wording, verbatim** |
| 46–65 | 4–5 | full reasoning, secondary points included |
| >65 | 5 | systematic, plus the lead-perspective clause |

Item counts stay inside the PRD's 3–5. The slider moves depth further than it
moves count, and that is deliberate: the count is bounded by a contract, so
stretching it to give the sliders more travel would have broken FR-07/08/09 to
win a presentation point. The middle band is the old instruction word for word,
which is what makes an omitted mix a genuine no-op.

The band sets `min_length`/`max_length` on the response envelope, so the bounds
reach the provider's decoder through the strict schema rather than being trimmed
after we have paid for the tokens. One consequence worth knowing: a heavy slider
asking for five items will *retry* against a model that returns three, and
`_salvage_items` keeps what it got when the retries do not help.

The heaviest slider also becomes the **lead perspective** and is told so — but
only when it leads the runner-up by 5 or more. A balanced 34/33/33 names nobody,
because handing one perspective every contested point on the strength of a
rounding remainder would be arbitrary.

### Repetition across the three sections, and what fixed it

The client reported the same point appearing under all three perspectives. It
was real, and structural: all three reason over one shared `VisualEvidence`, and
the domains genuinely overlap on the same physical facts — a weak focal point is
a composition fault, an attention fault and a hero-visibility fault at once. All
three reported it honestly and the reader saw one problem three times.

Two mechanisms, and only the second one holds:

1. **`SPECIALIST_BRIEFS` names an owner** for each contested concept — composition
   to creative_vm, the shopper to retail_psychology, the sale to commercial —
   with an explicit "what you do not own" clause naming the other two.
2. **`graph/nodes/reconcile.py` enforces it.** The specialists run in parallel
   and cannot see what they are about to duplicate, so the first point in the
   graph where a duplicate is even visible is after the fan-in. One batched
   embedding call over every item; the higher-weighted perspective keeps a
   contested point and the other's copy is dropped.

Same division of labour as the citation guard: the prompt asks, the code makes
it true.

The survivor records the loser in `also_raised_by`, shown in the UI as *"also
raised by Commercial"*. Three specialists independently reaching one finding is
a severity signal — deleting the duplicate without recording it would throw that
away with the noise.

#### Why there is no similarity threshold

A fixed cosine cutoff is the obvious design and it does not work. Measured on
two real analyses against hand-labelled duplicates:

| run | true duplicates | first genuinely distinct pair |
|---|---|---|
| balanced | .858 .836 .807 .785 | .775 |
| lopsided | .754 .719 | .683 |

The bands separate cleanly *within* a run but sit at different heights *between*
runs — a cutoff catching the lopsided run's duplicates at .719 would delete four
distinct findings from the balanced one. The absolute level tracks how much
vocabulary a given photograph's findings happen to share, which is not a
property of anything we control.

So the rule is relative to each analysis's own distribution, on **mean-centred**
vectors. Centring subtracts the "this display" component every finding shares —
the part that makes two unrelated findings score 0.6 — and roughly triples the
gap between a real duplicate and a merely adjacent finding. A pair is a
duplicate when it sits `duplicate_sigma` (3.5) deviations above the **median**,
measured by median absolute deviation.

**Median and MAD, not mean and standard deviation**, because both of those are
moved by the outliers being looked for. Two cases show why, and both were caught
in testing rather than in production:

- An analysis with *no* duplicates has a tight distribution, so its most similar
  pair is two standard deviations out by construction and gets struck. This is
  what `duplicate_similarity_floor` (0.20) also guards.
- An analysis where nearly *every* finding is duplicated inflates the standard
  deviation until the bar rises above every real duplicate and nothing at all is
  caught.

The robust form handles both. Verified across the two real runs plus six
synthetic distributions from zero duplicates to all-duplicates: every labelled
duplicate caught, no false positives. 3.5 is the middle of a working range of
about 3.0–3.9 on that set.

Three guards protect against over-deletion: no perspective drops below two items
(a one-line section reads as a broken agent, not a quiet one); the floor stops
the relative rule inventing duplicates; and a failed embedding call degrades to
no deduplication with a note in `errors` rather than failing the run.

**The embedding call is capped at 8 seconds** (`reconcile_embed_timeout_seconds`)
and abandoned if it overruns. It normally takes 2–4s, but a rate-limited
endpoint once backed off for 21.7 — a third of the latency target spent on
tidying. Deduplication is the one stage that can be skipped without changing
what the analysis concludes, so it is the one that gives up first.

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

**`capture="environment"` removes the photo library; it does not merely prefer
the camera.** On a phone, a file input carrying that attribute opens the rear
camera and offers no other route, so a merchandiser reviewing a photo taken
earlier has no way to reach it — while desktop, where `capture` is ignored,
looks perfectly fine. The capture screen therefore keeps two inputs, one with
the attribute and one without, and hides the camera button behind
`@media (pointer:coarse)` so that desktop is not offered two buttons that do
the same thing.

**Do not name HEIC in `accept`.** iOS transcodes HEIC to JPEG on upload unless
the accept attribute asks for HEIC, and Pillow — with no `pillow-heif` — cannot
decode HEIF at all. Naming the format means every iPhone photo arrives in
exactly the encoding the server then rejects as "the file is not a readable
image". `accept="image/*"` is both broader for the picker and narrower in what
actually arrives. If HEIC support is ever wanted for its own sake (an Android
or desktop user uploading a `.heic` file), add `pillow-heif` and register the
opener; the accept list is not where that gets fixed.

**Nothing may be added to `SpecialistItemDraft`.** That model is the wire schema:
`_schema_for` builds the strict structured-output schema from it, and
`strict_schema` makes every field required. A field the model has no way to know
about therefore becomes a field it is *forced to invent*. `also_raised_by` lives
one layer up on `SpecialistItem`, which the model never sees, and the lift from
draft to item happens at the end of `run_specialist`. Anything the pipeline
learns about an item after the model returns belongs there too.

**A hardcoded list of migrations in a test will rot.** `test_api_isolation.py`
listed the first four by name, so 0005, 0006 and 0007 were silently absent from
the test schema — a column that existed in production did not exist under test,
and the first thing that noticed was a query failing at runtime. It globs the
directory now. `test_retrieval_cap.py` and `test_tenant_session.py` still list
theirs deliberately: they build a schema *without* RLS on purpose, and globbing
would apply 0004 and break that.

---

## 8. Deployment to Railway

**Deployed and running**, Railway project `scintillating-expression`, EU West:

| | |
|---|---|
| Frontend | https://ravishing-courage-production-7828.up.railway.app |
| API | https://vmlab-phase12-production.up.railway.app |

Health checks: `/health` reports the environment, `/health/db` returns live
tenant and chunk counts. Neither requires a token, so both are safe to curl
when something looks wrong.

Sign-in does not work yet — see the SMTP gap in §10. The site loads and the
API answers; nobody outside the team can get past the login screen.

Two services from the one repo, each with its own **Root Directory** — that
setting is what makes the monorepo work. Without it Railpack inspects the repo
root, finds no Python and no `package.json`, and fails during "Build image"
having never looked inside `apps/`.

Build and start commands live in `apps/api/railway.json` and
`apps/web/railway.json`, so they are versioned rather than typed into a
dashboard nobody else can see. Railway reads that file from the service's root
directory; there is nothing to configure by hand beyond the root directory
itself and the variables.

### The API service

| Setting | Value |
|---|---|
| Root Directory | `apps/api` |
| Region | `europe-west4` (Amsterdam) — Railway has no London region; this is the closest to the `eu-west-2` database |
| Everything else | from `railway.json` |

Variables — all of §2, plus one that only matters here:

```
ENVIRONMENT=production
CORS_ALLOWED_ORIGINS=https://<the web service domain>
DATABASE_URL=...            # session pooler, password percent-encoded
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
OPENROUTER_API_KEY=...
```

Leave `LANGSMITH_TRACING` unset. Enabling it uploads retrieved corpus text —
the client's confidential material — to a third party.

**`CORS_ALLOWED_ORIGINS` is not optional in production.** The API and the
frontend are different origins once deployed, so with it empty every browser
request fails preflight and the UI looks broken while the API answers `curl`
perfectly. Startup logs a warning when production runs without it. It is
chicken-and-egg with the web service's domain: deploy the API, deploy the web
service, then come back and set this.

### The web service

| Setting | Value |
|---|---|
| Root Directory | `apps/web` |
| Region | same as the API |

```
NEXT_PUBLIC_API_URL=https://<the API service domain>
NEXT_PUBLIC_SUPABASE_URL=...
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
```

Next.js inlines `NEXT_PUBLIC_*` at **build** time, not at run time. Changing one
requires a redeploy, not a restart — and the API must have a domain before the
web build runs. Do not set `NEXT_PUBLIC_ALLOW_PASSWORD_LOGIN` in production.

### Supabase, after the domains exist

Authentication → URL Configuration: set **Site URL** to the web service's domain
and add it to **Redirect URLs**. The login page derives its magic-link redirect
from `window.location.origin`, so the code needs no change — but Supabase
refuses redirects to origins it does not know, and the link silently bounces.

Sign-in also needs real SMTP configured (§10). Until it is, nobody outside the
team can log in to the deployed instance.

### Order of operations

1. Deploy the API. Confirm `GET /health` returns `{"status":"ok"}` and
   `GET /health/db` reports the tenant and chunk counts.
2. Deploy the web service with `NEXT_PUBLIC_API_URL` pointing at it.
3. Set `CORS_ALLOWED_ORIGINS` on the API to the web domain; redeploy the API.
4. Add both URLs to Supabase auth configuration.
5. Re-run the §9 verification against the deployed environment. Passing locally
   is not the same claim.

### Two things that will bite

**Keep both services at one replica.** An analysis runs as a detached
`asyncio` task inside the process that accepted the upload
(`analyses.py:226`). A second replica cannot report progress for a run it is
not hosting, and a redeploy mid-analysis strands that row in `processing`
forever — there is no queue and no resume.

**The repo belongs to Abdul's GitHub account.** Deploying from GitHub needs the
Railway app installed on `abduld237/VMlab-phase1.2`, which only someone with
admin on that repo can authorise. `railway up` from the CLI uploads the working
tree directly and needs no GitHub connection at all.

**A trailing slash on `NEXT_PUBLIC_API_URL` used to break every request**, and
the symptom pointed nowhere near the cause. `https://…app/` builds
`https://…app//api/uploads`, which matches no route, so FastAPI answers with
its framework default `{"detail":"Not Found"}` — capitalised, unlike our own
lowercase `"not found"` — and the UI shows "Not Found" under the upload form as
though the record were missing. `lib/api.ts` now strips trailing slashes, so
the value is forgiving. The capitalisation is still the tell: **"Not Found"
means the route does not exist; "not found" means RLS returned no rows.**

**Give the web service only its three `NEXT_PUBLIC_` variables.** Pasting the
API's block into it puts `SUPABASE_SERVICE_ROLE_KEY`, `OPENROUTER_API_KEY` and
`DATABASE_URL` on a service that serves HTML and nothing else. Next.js inlines
only `NEXT_PUBLIC_*` into the bundle, so this does not leak to browsers — but
the service-role key bypasses RLS entirely and has no business being there.
Delete anything else from that service, `NEXT_PUBLIC_ALLOW_PASSWORD_LOGIN`
included (§10).

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

- **Deployed, but not yet verified end to end.** Both services run on Railway
  (URLs in §8) and the API reaches the live database, but the §9 checklist has
  only been run locally. Nothing has completed an analysis through the deployed
  frontend, because sign-in is still blocked on SMTP below.
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
  Delete both before any real pilot — and note that `tenant-b@vmlab.test` now
  shares Pilot Retailer Two with Abdul, so it can read anything he analyses.
  That is RLS working as designed, not a leak, but it is a test account sitting
  inside a real workspace and it should go.
- **The duplicate rule is calibrated on two analyses, not twenty.** Every
  hand-labelled duplicate in those two is caught with no false positives, and
  the synthetic cases cover the degenerate ends, but two photographs is a small
  sample for a threshold. Read all three sections of a few more real analyses
  before treating `duplicate_sigma` as settled. It currently under-removes by
  design: the weakest real duplicate measured (0.223 centred) sits close enough
  to the strongest distinct pair (0.232 in another run) that catching it
  reliably would mean deleting genuine findings.
- **The design deck has four specialists; the system has three.** The Priority
  Studio screen in `Presentation.pdf` adds a *Graphic Design* agent covering
  messaging, hierarchy and signage clarity. Three is wired into the
  `public.perspective` enum, the RLS policies, the schemas and the UI, so a
  fourth is a Phase 2 scope item rather than an omission. Say so rather than
  letting the client read three sliders against his four-slider design as an
  oversight.

---

## 11. Roles and provisioning

Two roles per tenant, `admin` and `user`, on `profiles`. Authorisation is the
database's: `brand_identity_admin_write` requires `is_tenant_admin()`, and the
route handler deliberately contains no role check of its own, so there is
nothing that can drift out of step with the policy.

**Provisioning is manual and deliberately so** (FR-16 — administrator-created
tenants, no self-service). `authenticated` has no `insert` on `profiles`, so a
new user must be assigned by someone with database access:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
     -v email=them@example.com -v tenant=pilot-three -v role=user \
     -v name='Their Name' -f db/provision_user.sql
```

A signed-in user with no profile row gets **403 "user is not assigned to a
workspace"** from `deps.py` before any data is touched. That is the intended
behaviour, not a bug.

**But treat it as a step in inviting someone, not as an error to respond to.**
Signing up and being provisioned are separate acts and nothing in the product
connects them: the login screen accepts anyone, issues a magic link, signs them
in, shows them the capture screen, and lets them choose a photo — and only when
they press *Start analysis*, at the first call that touches the API, do they
learn they have no workspace. Everything before that point works, which makes
the failure look like a fault in the upload rather than an account that was
never finished. Abdul hit exactly this on 30 August. Provision people the day
you invite them.

The workspace assignments as they stand: Abdul is admin of **Pilot Retailer
Two** (`pilot-two`), which was the empty one — the other two hold test
analyses. The tenant names are still the placeholders from
`0005_seed_tenants.sql`; renaming one to a real retailer is
`update public.tenants set name = '…' where slug = 'pilot-two'`, and nothing
references the name.

### A privilege escalation that was live, and how it was found

`profiles_update_self` originally let a user update their own row and checked
only that they did not change tenant. Nothing constrained `role`, so any
standard user could set themselves to `admin` and gain every admin permission in
their workspace — including rewriting the brand profile that conditions every
analysis the whole tenant runs.

It was not reachable only through our API. **Supabase publishes PostgREST at
`/rest/v1/` against the same tables under the same RLS**, so a user's own
session token plus the publishable anon key — both in the browser by design —
were enough:

```
PATCH /rest/v1/profiles?user_id=eq.<self>   {"role": "admin"}
```

Confirmed against the live project: 200, role `admin`, after which a brand write
refused with 403 seconds earlier succeeded. Fixed in migration
`0006_profile_privilege_escalation.sql` and asserted in `db/test/`.

**The lesson generalises.** Our API is not the only door to this database. Any
policy that is loose enough to matter is reachable directly over PostgREST with
credentials the browser already has. Review RLS policies as though the API did
not exist, and prefer `db/test/run.sh` — which drives SQL directly — over
API-level tests when checking an authorisation boundary.
