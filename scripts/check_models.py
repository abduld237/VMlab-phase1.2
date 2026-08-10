"""Preflight the configured model IDs against OpenRouter.

A stale model ID does not fail at startup -- it fails on the first real request,
which in this pipeline means partway through an analysis or several hundred
pages into an ingestion run. Run this after changing any model in config.py, and
in CI.

The chat and vision models are checked against the public /models catalogue and
need no API key. Embedding models are not listed there, so that one is verified
by actually embedding a short string, which does need OPENROUTER_API_KEY. Without
a key the script reports the embedding check as skipped rather than passing it.
"""

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from vmlab.config import get_settings  # noqa: E402
from vmlab.models.openrouter import OpenRouterClient, OpenRouterError  # noqa: E402

CATALOGUE = "https://openrouter.ai/api/v1/models"


async def check_catalogue(settings) -> list[str]:
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.get(CATALOGUE)
        response.raise_for_status()
        catalogue = {m["id"]: m for m in response.json()["data"]}

    for label, model_id in (
        ("vision_model", settings.vision_model),
        ("reasoning_model", settings.reasoning_model),
    ):
        entry = catalogue.get(model_id)
        if entry is None:
            failures.append(f"{label}: '{model_id}' is not in the OpenRouter catalogue")
            print(f"  FAIL  {label:<16} {model_id}")
            continue

        modalities = (entry.get("architecture") or {}).get("input_modalities", [])
        if label == "vision_model" and "image" not in modalities:
            failures.append(f"{label}: '{model_id}' does not accept image input")
            print(f"  FAIL  {label:<16} {model_id} (no image input)")
            continue

        price = float((entry.get("pricing") or {}).get("prompt") or 0) * 1e6
        print(f"  ok    {label:<16} {model_id}  ${price:.2f}/M in, ctx {entry.get('context_length')}")

    return failures


async def check_embeddings(settings) -> list[str]:
    if not settings.openrouter_api_key:
        print("  skip  embedding_model  (set OPENROUTER_API_KEY to verify)")
        return []

    try:
        async with OpenRouterClient(settings) as client:
            vectors = await client.embed(["vmlab preflight"])
    except OpenRouterError as exc:
        print(f"  FAIL  embedding_model   {settings.embedding_model}: {exc}")
        return [f"embedding_model: {exc}"]

    print(
        f"  ok    embedding_model  {settings.embedding_model}  "
        f"{len(vectors[0])} dimensions"
    )
    return []


async def main() -> int:
    settings = get_settings()
    print("Checking configured models against OpenRouter:")
    failures = await check_catalogue(settings)
    failures += await check_embeddings(settings)

    if failures:
        print(f"\n{len(failures)} problem(s) found:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nall configured models resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
