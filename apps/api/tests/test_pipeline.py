"""The analysis graph end to end, against a stub model client.

No OpenRouter key needed. What is being tested is the wiring, not the model:
that evidence runs once and reaches all three specialists, that the specialists
genuinely fan out, that costs and timings accumulate across parallel branches
rather than clobbering each other, and -- the important one -- that a single
failing specialist degrades to a partial result with an honest disclosure
instead of taking the whole analysis down.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vmlab.graph.pipeline import run_analysis  # noqa: E402
from vmlab.graph.schemas import Perspective  # noqa: E402
from vmlab.models.openrouter import Completion, OpenRouterError  # noqa: E402
from vmlab.retrieval.retriever import RetrievedChunk  # noqa: E402

EVIDENCE = {
    "display_type": "end-cap",
    "probable_location": "main aisle",
    "focal_point": "stacked cartons at eye level",
    "hero_product": "seasonal biscuit selection",
    "secondary_products": ["tea", "napkins"],
    "approximate_sku_count": 12,
    "signage_count": 2,
    "signage_legible": True,
    "price_visible": False,
    "dominant_colours": ["red", "gold"],
    "lighting_style": "overhead fluorescent",
    "fixtures": ["wire basket", "shelf riser"],
    "composition_notes": ["dense stacking", "no clear sightline"],
    "image_quality_notes": [],
    "occlusions": [],
    "observations": [
        {
            "observation_id": "OBS-0001",
            "entity_type": "signage",
            "attribute": "price visibility",
            "observed_value": "no price tickets visible on the front face",
            "inference_status": "Observed only",
            "model_confidence": 0.8,
            "evidence_sufficiency": 2,
        }
    ],
}

ITEMS = {
    "items": [
        {
            "observation": f"observation {i}",
            "recommendation": f"recommendation {i}",
            "reason": "because the evidence supports it",
            "confidence": 0.7,
            "severity": "medium",
            "supporting_rule_ids": ["PPS-032"],
        }
        for i in range(3)
    ]
}

SYNTHESIS = {
    "overall_summary": "Strong visual impact, weak price communication.",
    "uncertainty_note": None,
    "actions": [
        {
            "rank": i + 1,
            "action": f"action {i + 1}",
            "rationale": "improves decision confidence",
            "effort": "quick_win",
            "confidence": 0.7,
            "customer_impact": 4,
            "commercial_impact": 4,
            "urgency": 3,
            "effort_score": 2,
            "priority_score": 80 - i * 10,
            "contributing_perspectives": ["commercial"],
            "supporting_rule_ids": ["PAC-004"],
            "trade_off": None,
        }
        for i in range(3)
    ],
}


class StubClient:
    """Stands in for OpenRouterClient, recording what it was asked."""

    def __init__(self, failing: set[str] | None = None):
        self.failing = failing or set()
        self.vision_calls = 0
        self.embed_calls = 0
        self.specialist_prompts: list[str] = []

    async def describe_image(self, **kwargs):
        self.vision_calls += 1
        return Completion(
            text=json.dumps(EVIDENCE), model="stub-vision", provider="TestProvider",
            prompt_tokens=100, completion_tokens=50, cost_usd=0.001,
        )

    async def embed(self, texts, **kwargs):
        self.embed_calls += 1
        return [[0.0] * 1024 for _ in texts]

    async def complete(self, *, model, messages, **kwargs):
        # The vision call is the one carrying an image part, so its user content
        # is a list of blocks rather than a plain string. Evidence extraction
        # routes through complete() (not describe_image) so that it gets the
        # same schema-retry treatment as every other stage.
        user_content = messages[1]["content"] if len(messages) > 1 else ""
        if isinstance(user_content, list):
            self.vision_calls += 1
            return Completion(
                text=json.dumps(EVIDENCE), model="stub-vision", provider="TestProvider",
                prompt_tokens=100, completion_tokens=50, cost_usd=0.001,
            )

        body = messages[-1]["content"]

        if "SPECIALIST FINDINGS" in body:
            return Completion(
                text=json.dumps(SYNTHESIS), model="stub-text", provider="TestProvider",
                prompt_tokens=200, completion_tokens=80, cost_usd=0.002,
            )

        brief = messages[0]["content"]
        self.specialist_prompts.append(brief)
        for name in self.failing:
            # Matched against the brief's opening line, not against the whole
            # prompt. Each brief now names the other two perspectives when it
            # says what it does *not* own, so a bare "Retail Psychology" appears
            # in all three and would fail whichever specialist ran first.
            if f"you assess the {name}".lower() in brief.lower():
                raise OpenRouterError(f"stubbed failure for {name}")

        return Completion(
            text=json.dumps(ITEMS), model="stub-text", provider="TestProvider",
            prompt_tokens=150, completion_tokens=60, cost_usd=0.0015,
        )


async def test_full_pipeline_produces_a_complete_result():
    client = StubClient()

    result, telemetry = await run_analysis(
        client=client, retriever=None, image_bytes=b"fake", tenant_id=None
    )

    assert client.vision_calls == 1, "evidence must be extracted once, not per specialist"
    assert len(result.findings) == 3
    assert {f.perspective for f in result.findings} == set(Perspective)
    assert len(result.synthesis.actions) == 3
    assert result.synthesis.actions[0].rank == 1
    assert not telemetry["errors"]


async def test_costs_and_timings_accumulate_across_parallel_branches():
    client = StubClient()

    _, telemetry = await run_analysis(
        client=client, retriever=None, image_bytes=b"fake", tenant_id=None
    )

    # 1 vision + 3 specialists + 1 synthesis. A reducer that overwrote instead
    # of summing would report only the last branch's cost.
    assert telemetry["cost_usd"] == pytest.approx(0.001 + 3 * 0.0015 + 0.002)
    assert telemetry["prompt_tokens"] == 100 + 3 * 150 + 200

    for stage in ("evidence", "retrieval", "synthesis", "total"):
        assert stage in telemetry["stage_timings_ms"]
    for perspective in Perspective:
        assert perspective.value in telemetry["stage_timings_ms"]


async def test_a_failing_specialist_degrades_rather_than_aborting():
    client = StubClient(failing={"Retail Psychology"})

    result, telemetry = await run_analysis(
        client=client, retriever=None, image_bytes=b"fake", tenant_id=None
    )

    perspectives = {f.perspective for f in result.findings}
    assert Perspective.RETAIL_PSYCHOLOGY not in perspectives
    assert len(result.findings) == 2, "the other two specialists must still complete"
    assert telemetry["errors"], "the failure must be recorded, not swallowed"

    # And the user must be told, rather than being shown a partial result that
    # looks complete.
    assert result.synthesis.uncertainty_note
    assert "retail_psychology" in result.synthesis.uncertainty_note


async def test_each_specialist_receives_its_own_brief():
    client = StubClient()

    await run_analysis(client=client, retriever=None, image_bytes=b"fake", tenant_id=None)

    assert len(client.specialist_prompts) == 3
    joined = " ".join(client.specialist_prompts).lower()
    assert "visual hierarchy" in joined
    assert "cognitive load" in joined
    assert "cross-selling" in joined


async def test_a_muted_slider_asks_for_less_and_a_heavy_one_asks_for_more():
    """The slider has to change the instruction, or it changes nothing."""
    client = StubClient()

    await run_analysis(
        client=client,
        retriever=None,
        image_bytes=b"fake",
        tenant_id=None,
        priorities={"creative_vm": 70, "retail_psychology": 10, "commercial": 20},
    )

    by_perspective = {
        "creative_vm": next(p for p in client.specialist_prompts if "You own composition" in p),
        "retail_psychology": next(
            p for p in client.specialist_prompts if "You own the shopper" in p
        ),
        "commercial": next(p for p in client.specialist_prompts if "You own the sale" in p),
    }

    assert "Produce exactly 5 items" in by_perspective["creative_vm"]
    assert "Produce exactly 3 items" in by_perspective["retail_psychology"]
    assert "Produce exactly 3 items" in by_perspective["commercial"]


async def test_only_the_heaviest_perspective_is_told_it_leads():
    """A tie-break that names two winners is not a tie-break."""
    client = StubClient()

    await run_analysis(
        client=client,
        retriever=None,
        image_bytes=b"fake",
        tenant_id=None,
        priorities={"creative_vm": 70, "retail_psychology": 10, "commercial": 20},
    )

    # Deduplicated: a schema retry re-sends the same prompt, and the stub always
    # returns three items, so the specialist asked for five retries three times.
    leading = {p for p in client.specialist_prompts if "lead perspective" in p}
    assert len(leading) == 1
    assert "You own composition" in next(iter(leading))


async def test_a_balanced_mix_names_no_leader():
    client = StubClient()

    await run_analysis(client=client, retriever=None, image_bytes=b"fake", tenant_id=None)

    assert not [p for p in client.specialist_prompts if "lead perspective" in p]


async def test_an_omitted_mix_reproduces_the_pre_slider_prompt():
    """Adding the feature must be a no-op for anyone who ignores it."""
    client = StubClient()

    await run_analysis(client=client, retriever=None, image_bytes=b"fake", tenant_id=None)

    for prompt in client.specialist_prompts:
        assert "Three is a floor, not a target" in prompt


async def test_quality_flags_carry_into_the_result():
    client = StubClient()

    result, _ = await run_analysis(
        client=client,
        retriever=None,
        image_bytes=b"fake",
        tenant_id=None,
        quality_flags=["image appears soft or out of focus"],
    )

    assert result.low_confidence is True
    assert "image appears soft or out of focus" in result.evidence.image_quality_notes


class StubRetriever:
    """Returns one identifiable chunk per domain, so refs can be traced back."""

    async def search(self, *, domain, embedding, tenant_id, top_k):
        return [
            RetrievedChunk(
                id=f"chunk-{domain}-{index}",
                document_id="VMLAB-KB-12",
                title="Product Presentation Standards",
                domain=domain,
                authority="canonical",
                content="...",
                section_path=None,
                page=1,
                rule_ids=["PPS-001"],
                distance=0.1,
            )
            for index in range(2)
        ]


async def test_retrieved_chunks_are_reported_per_perspective():
    # The persist layer writes analyses.retrieved_chunk_ids and each section's
    # evidence_refs from this key. It was missing for the whole build, so every
    # completed analysis recorded an empty audit trail -- and an audit trail
    # nobody asserts on fails silently, which is the only way it can fail.
    client = StubClient()

    _, telemetry = await run_analysis(
        client=client, retriever=StubRetriever(), image_bytes=b"fake", tenant_id=None
    )

    refs = telemetry["evidence_refs"]
    assert set(refs) == {p.value for p in Perspective}
    assert refs["commercial"] == ["chunk-commercial-0", "chunk-commercial-1"]
    # The flat list is what the analyses row stores; it must cover every domain.
    assert len(telemetry["retrieved_chunk_ids"]) == 6


async def test_the_serving_provider_is_recorded_per_stage():
    # A model id is not a service: the same id is served by endpoints ranging
    # from 18 to 940 tokens/second, so the provider is the part that explains a
    # slow run after the fact.
    client = StubClient()

    _, telemetry = await run_analysis(
        client=client, retriever=None, image_bytes=b"fake", tenant_id=None
    )

    assert telemetry["model_versions"]["vision"] == "stub-vision via TestProvider"
    assert telemetry["stage_attempts"]["synthesis"] == 1
