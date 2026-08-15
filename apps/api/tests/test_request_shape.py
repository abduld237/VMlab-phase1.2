"""What we actually put on the wire, for the parts that control latency.

The pipeline's speed is decided by three fields in the request body rather than
by anything in the prompts: which providers may serve it, whether the response
shape is enforced by the decoder, and how much hidden reasoning the model does
first. Measured runs against the live corpus varied from 119s to 360s for
identical work purely on provider selection, so these are worth pinning: a
silently dropped `provider` block would look like nothing at all until someone
re-measured.
"""

from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vmlab.config import Settings  # noqa: E402
from vmlab.graph.schemas import Synthesis, VisualEvidence  # noqa: E402
from vmlab.models.openrouter import OpenRouterClient, strict_schema  # noqa: E402


def capturing_client(sent: list[dict]) -> httpx.AsyncClient:
    """An httpx client that records request bodies and returns a valid reply."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-oss-120b",
                "provider": "Groq",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
            },
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test"
    )


async def call_with(settings: Settings, **kwargs) -> dict:
    sent: list[dict] = []
    async with OpenRouterClient(settings=settings, client=capturing_client(sent)) as client:
        await client.complete(model="m", messages=[{"role": "user", "content": "hi"}], **kwargs)
    return sent[0]


# -- provider routing -------------------------------------------------------


async def test_requests_are_routed_by_throughput_under_a_price_ceiling():
    payload = await call_with(Settings(provider_sort="throughput"))

    assert payload["provider"]["sort"] == "throughput"
    assert payload["provider"]["max_price"] == {"prompt": 0.15, "completion": 0.60}
    assert payload["provider"]["require_parameters"] is True


async def test_fallbacks_are_left_enabled():
    # A provider going down should cost latency, not the analysis. Asserting the
    # key is absent rather than false: the default is what we are relying on.
    payload = await call_with(Settings())
    assert "allow_fallbacks" not in payload["provider"]


async def test_routing_can_be_switched_off():
    payload = await call_with(Settings(provider_sort=""))
    assert "provider" not in payload


async def test_price_ceiling_is_configurable():
    payload = await call_with(
        Settings(provider_max_price_prompt=1.5, provider_max_price_completion=4.0)
    )
    assert payload["provider"]["max_price"] == {"prompt": 1.5, "completion": 4.0}


# -- structured outputs -----------------------------------------------------


async def test_a_strict_schema_is_sent_when_one_is_supplied():
    schema = strict_schema(Synthesis.model_json_schema())
    payload = await call_with(Settings(), json_schema=schema, schema_name="synthesis")

    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "synthesis"


async def test_json_object_is_the_fallback_when_no_schema_is_given():
    payload = await call_with(Settings(), json_object=True)
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("model", [VisualEvidence, Synthesis])
def test_strict_schemas_carry_no_keyword_the_strict_subset_rejects(model):
    # Strict mode rejects validation keywords outright rather than ignoring
    # them, so one stray "minimum" from a pydantic Field(ge=...) fails the whole
    # request with a 400.
    text = json.dumps(strict_schema(model.model_json_schema()))
    for keyword in ("minimum", "maximum", "minItems", "maxItems", "default", "pattern"):
        assert f'"{keyword}"' not in text


def test_optional_fields_become_required_and_nullable():
    # Strict mode has no optional fields. An optional value has to be demanded
    # and allowed to be null instead, or the provider rejects the schema.
    schema = strict_schema(Synthesis.model_json_schema())

    assert schema["required"] == list(schema["properties"])
    assert schema["additionalProperties"] is False
    assert {"type": "null"} in schema["properties"]["uncertainty_note"]["anyOf"]


def test_nested_definitions_are_tightened_too():
    # The bug this guards against is tightening only the top level and leaving
    # $defs untouched, which passes a cursory look and fails at the provider.
    schema = strict_schema(Synthesis.model_json_schema())
    action = schema["$defs"]["PrioritisedAction"]

    assert action["additionalProperties"] is False
    assert action["required"] == list(action["properties"])


# -- reasoning effort -------------------------------------------------------


async def test_reasoning_effort_is_sent_when_configured():
    payload = await call_with(Settings(), reasoning={"effort": "low"})
    assert payload["reasoning"] == {"effort": "low"}


async def test_reasoning_can_be_disabled_outright():
    # The vision stage does not need deliberation, and on a 24 tok/s endpoint
    # thinking tokens are the bulk of the wait.
    payload = await call_with(Settings(), reasoning={"enabled": False})
    assert payload["reasoning"] == {"enabled": False}


async def test_no_reasoning_key_when_unset():
    # Absent means "the model's own default", which is not the same as low.
    payload = await call_with(Settings())
    assert "reasoning" not in payload


# -- provider attribution ---------------------------------------------------


async def test_the_serving_provider_is_recorded():
    sent: list[dict] = []
    async with OpenRouterClient(settings=Settings(), client=capturing_client(sent)) as client:
        completion = await client.complete(model="m", messages=[])

    assert completion.provider == "Groq"
