"""Runtime configuration, read from the environment.

Everything the client is paying for runs through OpenRouter, so there is one
model gateway and one key here rather than a provider per capability. Model IDs
are configurable because swapping models without touching pipeline code was an
explicit promise in the technical approach document (§3).
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo root, four levels up from apps/api/vmlab/config.py. Resolved from
# __file__ rather than the working directory so `uvicorn` started from apps/api
# and `pytest` started from the root both find the same file.
#
# Deployment flattens this. Railway's Root Directory setting copies apps/api to
# the image root, leaving only /app/vmlab/config.py -- three levels, and
# parents[3] is an IndexError raised at import, before logging exists to report
# it. There is no repo root there and nothing needs one: the .env files below
# do not exist in a container, and injected environment variables outrank them
# anyway. So fall back to the deepest parent rather than failing to start.
_CONFIG_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT = _CONFIG_PARENTS[3] if len(_CONFIG_PARENTS) > 3 else _CONFIG_PARENTS[-1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Repo-root .env first, then a local override beside the API if one
        # exists. Real environment variables still win over both, which is what
        # makes Railway's injected config authoritative in deployment.
        env_file=(_REPO_ROOT / ".env", _REPO_ROOT / "apps" / "api" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"

    # Browser origins allowed to call this API, comma separated. Empty in
    # development, where any origin is accepted; required in production, where
    # the deployed frontend is a different origin to the API and every request
    # fails preflight without it. Set it to the web service's public URL, e.g.
    # `https://vmlab-web-production.up.railway.app`.
    cors_allowed_origins: str = ""

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql://postgres:vmlab@localhost:55432/vmlab"

    # --- Supabase ----------------------------------------------------------
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # Used to verify access tokens. Without it the API cannot authenticate, and
    # deliberately fails closed rather than skipping signature verification.
    supabase_jwt_secret: str = ""
    # Server-side only. Bypasses RLS, so it must never reach the browser --
    # ingestion and admin tasks use it, request handling never does.
    supabase_service_role_key: str = ""
    storage_bucket: str = "display-uploads"

    # --- OpenRouter --------------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # All three IDs were checked against OpenRouter's live /models listing on
    # 2026-08-04. Verify again before changing one -- the catalogue moves fast
    # and a stale ID fails at request time, not at startup.

    # Vision model for evidence extraction: one call per analysis, and for a
    # long time the single slowest stage in the pipeline by a wide margin.
    #
    # Measured on six benchmark photographs, mean wall time per call:
    #
    #   qwen/qwen3.7-flash                44.1s   6-7 observations
    #   qwen/qwen3.7-flash, thinking off  23.7s   5 observations, thinner notes
    #   google/gemini-2.5-flash-lite       6.1s   6-7 observations
    #
    # qwen is served by exactly one endpoint at 24 tokens/second, so there is no
    # routing fix for it and its reasoning-effort setting is ignored -- only
    # disabling thinking outright moves it, and that costs evidence detail.
    # gemini-2.5-flash-lite is served by five endpoints, honours the controlled
    # vocabulary in the schema more faithfully, and supports strict structured
    # outputs, which removes the retry risk from the most expensive call.
    #
    # It costs $0.10/M in against qwen's $0.03/M -- about $0.0004 more per
    # analysis, which is noise inside the $0.01-0.05 quoted to the client.
    #
    # One known trait: it reports model_confidence as 1.0 on almost every
    # observation, where qwen discriminates between 0.8 and 0.95. That affects
    # the internal evidence record, not the confidence shown to the user, which
    # comes from the specialists. Worth revisiting if the evidence confidence
    # ever drives a decision.
    #
    # Any replacement must support structured outputs, or the strict-schema
    # request below will find no eligible provider and fail with a 404.
    vision_model: str = "google/gemini-2.5-flash-lite"
    # The three specialists and the synthesiser run over extracted evidence
    # rather than the image, so a text-only model is both cheaper and better at
    # holding to an output schema. Open-weight, Apache 2.0, $0.04/M in.
    reasoning_model: str = "openai/gpt-oss-120b"
    # Open-weight, 1024 dimensions -- fits pgvector's 2000-dim HNSW ceiling
    # natively. Changing this requires re-embedding the corpus and a migration.
    # Embedding models are not returned by /models, so this one is taken from
    # OpenRouter's embeddings collection and confirmed by scripts/check_models.py.
    embedding_model: str = "baai/bge-m3"
    embedding_dimensions: int = 1024

    # --- Provider routing ---------------------------------------------------
    # One model id is not one service. openai/gpt-oss-120b is served by twenty
    # endpoints whose measured throughput spans 18 to 940 tokens/second, and
    # OpenRouter routes by *price* unless told otherwise -- so the default picks
    # from the slow tail. That, not the prompts, is what made the same analysis
    # take 119s one run and 360s the next: identical work, identical token
    # counts, whichever cheap provider happened to catch the call.
    #
    # Sorting by throughput with a price ceiling buys the fast mid-tier
    # (Groq, Nebius, BaseTen at 240-395 tok/s) while excluding the premium tier.
    # Prices are US dollars per million tokens. Set provider_sort to "" to
    # restore OpenRouter's default routing.
    provider_sort: str = "throughput"
    provider_max_price_prompt: float = 0.15
    provider_max_price_completion: float = 0.60
    # Only route to providers that honour every parameter we send. Without it a
    # request asking for a strict JSON schema can land on a provider that
    # ignores it and answers in prose.
    provider_require_parameters: bool = True

    # How hard the reasoning model thinks before answering. Empty leaves it at
    # the model's own default, which is deliberately what we do.
    #
    # "low" was tried and measured against the default on the same eight images:
    # it saved 2.5s (p50 15.1s against 17.6s) and a third of the cost, but
    # returned 17% fewer citations (31.6 per run against 38.0) and dropped the
    # thinnest perspective from four items to three. Once routing made both
    # options finish in under twenty seconds, spending 2.5s on better-supported
    # findings was the easy trade -- the client judges this on whether the
    # recommendations are useful, and a citation is what makes one checkable.
    #
    # Set to "low" if latency ever becomes tight again; it is the cheapest
    # remaining lever and it costs evidence quality rather than correctness.
    reasoning_effort: str = ""
    # Strict provider-side schema enforcement, so a malformed response cannot
    # happen rather than being caught and retried. Every retry is a duplicate
    # call, and the retry tail is where the worst latency lives.
    use_strict_schemas: bool = True

    # --- Pipeline limits ---------------------------------------------------
    # The client's own Photo Standards sheet specifies a 1600px long edge, so
    # the pipeline resizes to match what its knowledge base was written around.
    image_long_edge_px: int = 1600
    max_upload_bytes: int = 15 * 1024 * 1024
    # PRD §8 targets a result inside 60s. Measured over all twenty benchmark
    # photographs from Dhaka against the live knowledge base: p50 14.5s, p90
    # 18.1s, slowest 20.5s, 20 of 20 inside the target.
    #
    # This is the backstop before a run is abandoned, not the expected duration.
    # 120s is roughly six times the slowest measured run, which leaves room for
    # a bad provider day without letting a genuinely stuck analysis sit for
    # seven minutes. It was 420s when a normal run took 200-360s.
    #
    # A previous version of this comment blamed the latency on the Dhaka-to-
    # London database round trip. That was wrong, and worth recording as wrong:
    # retrieval was 6-8s of a 119-360s run. The cost was provider routing --
    # OpenRouter sorts by price by default, and the cheapest endpoints serving
    # our reasoning model run at 18-25 tokens/second against Groq's 395.
    analysis_timeout_seconds: int = 120
    retrieval_top_k: int = 8

    # When two findings from *different* perspectives are the same point, so the
    # lower-weighted one is dropped and the survivor credits it. See
    # graph/nodes/reconcile.py.
    #
    # A fixed cosine threshold was tried first and does not work, which is worth
    # recording because it is the obvious thing to reach for again. Measured on
    # two real analyses against hand-labelled duplicates:
    #
    #   run          true duplicates      first genuinely distinct pair
    #   balanced     .858 .836 .807 .785  .775
    #   lopsided     .754 .719            .683
    #
    # The bands do not overlap *within* a run, but they sit at different heights
    # *between* runs -- a threshold catching the lopsided run's duplicates at
    # .719 would delete four distinct findings from the balanced one. There is
    # no single number that works, because the absolute level tracks how much
    # vocabulary a given photograph's findings happen to share.
    #
    # So the comparison is relative to each analysis's own spread, on
    # mean-centred vectors -- centring removes the "this display" component that
    # every finding shares and that inflates the baseline. A pair is a duplicate
    # when it sits at least `duplicate_sigma` deviations above the *median* of
    # that analysis's cross-perspective pairs, measured by median absolute
    # deviation and scaled by 1.4826 so the number reads as a normal sigma.
    #
    # Median and MAD rather than mean and standard deviation, because both of
    # those are moved by the very outliers being looked for. Two cases show it:
    # an analysis with no duplicates has a tight distribution, so its most
    # similar pair sits two standard deviations out by construction and gets
    # struck; and an analysis where nearly every finding is duplicated inflates
    # the standard deviation so far that the bar rises above every real
    # duplicate and nothing is caught at all. The robust statistics handle both
    # -- verified on the two runs above plus six synthetic distributions from
    # zero duplicates to all-duplicates, with every labelled duplicate caught
    # and no false positives.
    #
    # 3.5 is the middle of a working range of roughly 3.0 to 3.9 on that set,
    # rather than a value fitted to it. Lower it to remove more, raise it to
    # remove less. Recalibrate by reading all three sections of real analyses;
    # stub embeddings tell you nothing.
    duplicate_sigma: float = 3.5
    # The floor is what stops a relative rule manufacturing duplicates in a
    # report that has none: some pair is always the furthest out. Nothing below
    # this is a duplicate however far from its own distribution it sits. 0.20
    # sits under the weakest real duplicate measured (0.223) and over the
    # strongest genuinely distinct pair in the run that produced it (0.075).
    duplicate_similarity_floor: float = 0.20
    # How long to wait for the one embedding call reconciliation needs before
    # giving up on it. Measured at 2.3s, 3.5s and once 21.7s -- that last one on
    # a rate-limited endpoint backing off, which is exactly the case worth
    # capping. A tidier report is not worth a third of the 60s budget, and the
    # stage already degrades to passing findings through untouched.
    reconcile_embed_timeout_seconds: float = 8.0

    # --- Tracing -----------------------------------------------------------
    # LangSmith is off unless explicitly switched on. It is a debugging tool,
    # not part of the product: when enabled, every graph run -- including the
    # retrieved knowledge-base excerpts carried in the prompts -- is uploaded
    # to LangChain's servers. That corpus is the client's confidential
    # material, so the default has to be off and the switch has to be obvious.
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "vmlab-phase1"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
