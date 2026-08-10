"""OpenRouter client: the single gateway for vision, reasoning and embeddings.

Keeping all three behind one client is what makes the promise in the technical
approach document (§3) true in practice -- swapping a model is a config change,
not a code change, because nothing above this module knows which provider is
actually serving a request.

Costs are read back from OpenRouter's usage accounting rather than estimated
locally, so the per-analysis figure reported to the client is measured.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from vmlab.config import Settings, get_settings

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    """A request to OpenRouter failed in a way the caller cannot retry blindly."""


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> Any:
        """Parse the response as JSON, tolerating a fenced code block.

        Models wrap JSON in ```json fences often enough that treating it as a
        parse failure would mean retrying requests that actually succeeded.
        """
        text = self.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenRouterError(f"model did not return valid JSON: {exc}") from exc


class OpenRouterClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> OpenRouterClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.openrouter_base_url,
                timeout=httpx.Timeout(120.0, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                    # OpenRouter attributes usage to these; they show up in the
                    # client's dashboard, which is his account, so they matter.
                    "HTTP-Referer": "https://vmlab.app",
                    "X-Title": "VMlab Phase 1",
                },
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("use OpenRouterClient as an async context manager")
        return self._client

    # -- chat ---------------------------------------------------------------

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        json_object: bool = False,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Ask OpenRouter to return real token counts and cost rather than
            # us guessing from the text length.
            "usage": {"include": True},
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}

        response = await self.client.post("/chat/completions", json=payload)
        if response.status_code >= 400:
            raise OpenRouterError(f"{model} returned {response.status_code}: {response.text[:400]}")

        body = response.json()
        if "choices" not in body or not body["choices"]:
            raise OpenRouterError(f"{model} returned no choices: {str(body)[:400]}")

        usage = body.get("usage") or {}
        return Completion(
            text=body["choices"][0]["message"].get("content") or "",
            model=body.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost") or 0.0),
            raw=body,
        )

    async def describe_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 3000,
    ) -> Completion:
        """Single vision call over an already-resized image."""
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return await self.complete(
            model=model or self.settings.vision_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            max_tokens=max_tokens,
            json_object=True,
        )

    # -- embeddings ---------------------------------------------------------

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed a batch of texts.

        Returned in request order. Callers rely on that to zip embeddings back
        onto their chunks, so the ordering is re-established from each item's
        index rather than trusted to arrive sorted.
        """
        if not texts:
            return []

        # Bulk ingestion embeds thousands of chunks back to back and reliably
        # trips provider rate limits partway through. Without backoff a whole
        # document is lost to one 429 -- which is exactly what happened on the
        # first full corpus run, costing 7 documents including the largest.
        payload = {"model": model or self.settings.embedding_model, "input": texts}
        delay = 2.0
        for attempt in range(5):
            response = await self.client.post("/embeddings", json=payload)
            if response.status_code not in (429, 502, 503, 529):
                break
            if attempt == 4:
                raise OpenRouterError(
                    f"embeddings still rate-limited after 5 attempts "
                    f"({response.status_code})"
                )
            # Honour Retry-After when the provider sends one; otherwise back off
            # exponentially.
            wait = float(response.headers.get("retry-after") or delay)
            logger.warning(
                "embeddings %s, retrying in %.0fs (attempt %d/5)",
                response.status_code, wait, attempt + 1,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30.0)

        if response.status_code >= 400:
            raise OpenRouterError(f"embeddings returned {response.status_code}: {response.text[:400]}")

        data = response.json().get("data") or []
        if len(data) != len(texts):
            raise OpenRouterError(f"expected {len(texts)} embeddings, got {len(data)}")

        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in ordered]

        expected = self.settings.embedding_dimensions
        if vectors and len(vectors[0]) != expected:
            raise OpenRouterError(
                f"embedding model returned {len(vectors[0])} dimensions, "
                f"but the schema expects {expected} -- re-embedding and a migration "
                f"are both needed to change this"
            )
        return vectors
