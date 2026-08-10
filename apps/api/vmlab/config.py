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
_REPO_ROOT = Path(__file__).resolve().parents[3]


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

    # Vision model for evidence extraction: one call per analysis on a resized
    # image, so it is the single most expensive step and it sets the whole
    # per-analysis figure.
    #
    # Qwen's current flagship is qwen3.8-max, but at $2.00/M in and $6.00/M out
    # it costs about $0.0108 per vision call and pushes the total to ~$0.0118 --
    # inside the $0.01-0.05 quoted to the client, but only just. qwen3.7-flash
    # is one release behind, keeps the same 1M context, and costs $0.03/M in,
    # which brings the total back to ~$0.0012 and leaves the quoted range with
    # real headroom at pilot volume.
    #
    # Swap to "qwen/qwen3.8-max" if evidence quality proves insufficient on the
    # benchmark set -- that is the trade this line is making, and it should be
    # decided on measured output rather than assumed.
    vision_model: str = "qwen/qwen3.7-flash"
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

    # --- Pipeline limits ---------------------------------------------------
    # The client's own Photo Standards sheet specifies a 1600px long edge, so
    # the pipeline resizes to match what its knowledge base was written around.
    image_long_edge_px: int = 1600
    max_upload_bytes: int = 15 * 1024 * 1024
    # PRD §8 targets a result inside 60s where practical and allows longer
    # when progress is communicated, which the UI does. This is the hard
    # ceiling before a run is abandoned, not the expected duration.
    analysis_timeout_seconds: int = 180
    retrieval_top_k: int = 8

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
