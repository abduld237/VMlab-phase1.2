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


# Strict structured-output schemas are a restricted subset of JSON Schema:
# validation keywords are rejected outright rather than ignored, so a schema
# generated straight from pydantic fails the request with a 400.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
        "default",
    }
)


def strict_schema(schema: Any) -> Any:
    """Rewrite a pydantic JSON schema into the strict structured-output subset.

    Three rules, applied everywhere in the tree: no unsupported validation
    keywords, no additional properties, and every declared property listed as
    required. The last is the surprising one -- strict mode has no concept of an
    optional field, so a field that pydantic made optional must be *required and
    nullable* instead. Pydantic already emits `anyOf: [T, null]` for `X | None`,
    and fields that merely carry a default are safe to demand outright because
    the model can return the empty value.

    The bounds this drops (confidence 0-1, rank 1-3) are not lost: pydantic still
    validates the parsed response, so an out-of-range value fails there exactly
    as it did before. Losing them from the schema costs a hint, not a guarantee.
    """
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned = {
        key: strict_schema(value)
        for key, value in schema.items()
        if key not in _UNSUPPORTED_KEYWORDS
    }

    if isinstance(cleaned.get("properties"), dict):
        cleaned["additionalProperties"] = False
        cleaned["required"] = list(cleaned["properties"])

    return cleaned


@dataclass
class Completion:
    text: str
    model: str
    # Which upstream actually served this call. OpenRouter picks per request from
    # a pool whose measured throughput spans 18 to 940 tok/s for one model id, so
    # without recording this a slow analysis is indistinguishable from a slow
    # model -- which is exactly the confusion that hid a 20x latency spread.
    provider: str = ""
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
                # Read timeout sits above the slowest single call, not above a
                # whole analysis; the ceiling in Settings bounds the latter.
                #
                # 90s is generous against measured calls -- the slowest stage is
                # now evidence extraction at 5-12s -- but a single call is
                # exactly where a bad provider hurts, and allow_fallbacks only
                # helps when the request fails rather than crawls. It was 240s
                # when synthesis routinely took 106-190s.
                timeout=httpx.Timeout(90.0, connect=30.0),
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

    def _routing(self) -> dict[str, Any] | None:
        """Provider preferences for one request, or None to accept the default.

        Left unset, OpenRouter picks the cheapest endpoint serving a model. For
        openai/gpt-oss-120b that means choosing from a pool spanning 18 to 940
        tokens per second, which is how one analysis took 119s and the next 360s
        for identical work. Sorting by throughput under a price ceiling makes the
        speed predictable and the cost bounded.
        """
        settings = self.settings
        if not settings.provider_sort:
            return None
        return {
            "sort": settings.provider_sort,
            "max_price": {
                "prompt": settings.provider_max_price_prompt,
                "completion": settings.provider_max_price_completion,
            },
            "require_parameters": settings.provider_require_parameters,
            # Deliberately leaving allow_fallbacks at its default of true: a
            # provider going down should cost latency, not the whole analysis.
        }

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int = 2000,
        json_object: bool = False,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        reasoning: dict[str, Any] | None = None,
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
        # A strict schema is enforced by the provider's decoder, so the response
        # cannot be malformed; json_object only asks for "some JSON" and leaves
        # the shape to chance, and a wrong shape costs a full retry.
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            }
        elif json_object:
            payload["response_format"] = {"type": "json_object"}

        if reasoning is not None:
            # Hidden reasoning tokens are generated at the same rate as visible
            # ones, so on a slow endpoint they are pure latency. Takes either
            # {"effort": "low"} to shorten the thinking or {"enabled": False} to
            # turn it off for a stage that does not need it.
            payload["reasoning"] = reasoning

        routing = self._routing()
        if routing is not None:
            payload["provider"] = routing

        # Retry transient failures. Without this a single dropped connection
        # ends an analysis that has already paid for every stage before it --
        # a real run died on a ConnectTimeout during evidence extraction while
        # the provider was healthy a second later. Only network faults and the
        # provider's own retryable statuses qualify; a 400 is our bug and
        # repeating it just costs money.
        delay = 2.0
        for attempt in range(3):
            try:
                response = await self.client.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise OpenRouterError(
                        f"{model} unreachable after 3 attempts: {type(exc).__name__}"
                    ) from exc
                logger.warning(
                    "%s transport error (%s), retrying in %.0fs (attempt %d/3)",
                    model, type(exc).__name__, delay, attempt + 1,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 20.0)
                continue

            if response.status_code not in (429, 502, 503, 529):
                break
            if attempt == 2:
                break
            wait = float(response.headers.get("retry-after") or delay)
            logger.warning(
                "%s returned %s, retrying in %.0fs (attempt %d/3)",
                model, response.status_code, wait, attempt + 1,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, 20.0)

        if response.status_code >= 400:
            raise OpenRouterError(f"{model} returned {response.status_code}: {response.text[:400]}")

        body = response.json()
        if "choices" not in body or not body["choices"]:
            raise OpenRouterError(f"{model} returned no choices: {str(body)[:400]}")

        usage = body.get("usage") or {}
        return Completion(
            text=body["choices"][0]["message"].get("content") or "",
            model=body.get("model", model),
            provider=body.get("provider") or "",
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
