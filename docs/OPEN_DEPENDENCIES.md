# Open dependencies — things the build needs that we don't have

Tracked here so they stay visible rather than being discovered late. None of
these block Week 1–2 work; all of them block the Month-1 acceptance demo.

---

## 1. Benchmark image set — BLOCKING for acceptance

**Needed by:** ~18 August (Week 3, when the full pipeline runs end to end).

The PRD (§11.1 and the §11.2 acceptance checklist) requires a fixed set of real
retail-display photographs covering **strong, average and poor displays plus at
least one low-quality image**, and requires that **at least five complete
successfully**. The same set must be re-runnable so repeat analyses can be
compared, which is how §2.3 "repeatability" gets evidenced.

**We do not have these images.** Two sources were checked and both came up short:

- Abdul said in his 16 July email that he had attached reference display images.
  He had not. They were never resent and the point has not been raised since the
  Month-1 PDF pivot.
- The 268 MB corpus he did send contains **no in-store photography**. The three
  PPTX decks hold campaign artwork, logos and CGI room mockups; the brand VM
  guideline PDFs hold floor plans, vector prop drawings and rendered
  planograms. Verified by sampling all three decks plus the two most
  photographic PDFs.

**What we have instead:** 92 rendered planograms extracted to `data/fixtures/`
by `scripts/extract_display_fixtures.py` — Clinique counters, window schemes,
fixture layouts. These are fine for exercising the pipeline during development
and genuinely contain hero products, signage, shelving and product hierarchy.

They are **not** a valid benchmark set. Renders are idealised by construction —
even lighting, no clutter, no execution faults — so they cannot represent
"average", "poor" or "low-quality", and a pipeline tuned only on them will look
better in testing than it does on a real photo from a shop floor.

**Options, best first:**

1. **Photograph displays directly.** Five to ten photos from local retail, shot
   to the client's own Photo Standards sheet (eye-level ~1.5–1.7 m, display
   filling 80–90% of frame, 1600 px long edge, JPG 80–85%, 300–800 KB).
   Deliberately include one weak display and one poor-quality shot. A couple of
   hours' work, no external dependency, and it gives full control over the
   strong/average/poor spread.
2. **Ask Abdul for the images he originally promised** — worth doing regardless,
   since his own reference examples define the standard he's judging against.
   Cannot be relied on for timing: he has not replied since 23 July.
3. Public retail-display imagery, as a last resort — licensing would need
   checking and it would not reflect the pilot retailers.

Option 1 is the recommendation; option 2 should run in parallel as an email ask.

---

## 2. Missing canonical documents 13, 15 and 28

**Impact:** degraded retrieval on specific routes. Not blocking.

The corpus's own cross-reference tables (Doc 29 §19.1) route several question
types through documents that are absent from the package:

- **Document 13** — Hero Products & Commercial Prioritisation
- **Document 15** — Product Collections & Storytelling
- **Document 28** — Retail Scenarios & Worked Commercial Examples

Routes referencing them are currently unsatisfiable — notably *product
collection opportunity* (12, 13, 15, 17, 18, 22, 27) and *campaign performance*
(16, 17, 18, 22, 23, 28). Document 28 matters most: it is the worked-scenarios
corpus, which is exactly the material that makes a specialist's reasoning read
as grounded rather than generic.

Worth one line in the next email to Abdul. The pipeline degrades gracefully
without them.

---

## 3. Embeddings — RESOLVED, no longer an issue

Recorded because an earlier draft of this file wrongly flagged it as a gap.

OpenRouter **does** serve embeddings at `/api/v1/embeddings`, OpenAI-shaped, so
the whole stack stays behind the single gateway the client was told about. No
second provider, no separate key, one bill.

Chosen model: **`baai/bge-m3`** — open-weight, 1024 dimensions, 8K context,
$0.01 per million tokens. Embedding the full ~3,647-page corpus costs roughly
$0.02.

Dimension was the deciding factor. pgvector's HNSW index supports at most 2000
dimensions; Qwen3 Embedding 8B is 4096 natively and the 4B variant 2560, so
either would need Matryoshka truncation before it could be indexed. bge-m3 fits
natively at 1024.

---

## 4. Account access confirmation

**Impact:** blocks deployment, not development.

Abdul has not replied since 23 July, so the outcome of the proposed final-week-
of-July setup meeting is unconfirmed. The four accounts (GitHub, Supabase,
Railway, OpenRouter) are assumed created under his ownership per the setup guide
sent on 25 July.

Fallback if they are not live: local Docker Postgres + pgvector and a personal
OpenRouter key, migrating once the real accounts exist. Schema and RLS work is
identical either way, so nothing stalls.

---

## 5. Payment mechanism

**Impact:** commercial, not technical.

The fee is settled in writing (BDT 43,000, confirmed 23 July). The mechanism is
not: Abdul's message offered "bank transfer via Western Union" or "Western
Union" as though they were alternatives. EBL bank details were sent on 25 July;
one clear answer is still outstanding.
