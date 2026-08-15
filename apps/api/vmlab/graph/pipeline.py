"""The analysis graph.

    validate -> extract_evidence -> retrieve -> [3 specialists in parallel] -> synthesise

Evidence extraction runs once and feeds all three specialists. That was the
design the client endorsed in his 16 July email, and it is also what keeps the
perspectives genuinely distinct: each specialist sees the same facts but only its
own domain's knowledge, so the three outputs differ in substance rather than
being three rewordings of one answer.

Image validation happens before the graph, in the API layer, because a rejected
image should never become an analysis row at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from vmlab.config import get_settings
from vmlab.graph.nodes.reasoning import extract_evidence, run_specialist, synthesise
from vmlab.graph.schemas import AnalysisResult, Perspective
from vmlab.graph.state import AnalysisState
from vmlab.models.openrouter import OpenRouterClient
from vmlab.retrieval.retriever import Retriever

logger = logging.getLogger(__name__)


async def retrieve_knowledge(
    state: AnalysisState, client: OpenRouterClient, retriever: Retriever | None
) -> dict:
    """Stage 2: one domain-filtered query per specialist.

    The query text is built from the evidence rather than the raw user context,
    so retrieval reflects what is actually in the photograph. All three domains
    share a single embedding call -- they ask the same question of different
    corners of the corpus.
    """
    started = time.monotonic()

    if retriever is None:
        logger.warning("no retriever configured; specialists will run unsupported")
        return {
            "retrieved": {p.value: [] for p in Perspective},
            "stage_timings_ms": {"retrieval": 0},
        }

    evidence = state["evidence"]
    query = " ".join(
        part
        for part in [
            evidence.display_type,
            evidence.focal_point,
            evidence.hero_product,
            " ".join(evidence.composition_notes[:3]),
            " ".join(evidence.dominant_colours[:3]),
            state.get("campaign_objective") or "",
        ]
        if part
    )

    embeddings = await client.embed([query])
    vector = embeddings[0]

    retrieved: dict[str, list] = {}
    for perspective in Perspective:
        retrieved[perspective.value] = await retriever.search(
            domain=perspective.value,
            embedding=vector,
            tenant_id=state.get("tenant_id"),
            top_k=get_settings().retrieval_top_k,
        )

    total = sum(len(v) for v in retrieved.values())
    logger.info("retrieved %d chunks across %d domains", total, len(retrieved))

    return {
        "retrieved": retrieved,
        "stage_timings_ms": {"retrieval": int((time.monotonic() - started) * 1000)},
    }


StageCallback = Any  # Callable[[str], Awaitable[None]] | None


async def _announce(on_stage: StageCallback, stage: str) -> None:
    """Report a stage transition, never letting reporting break the analysis."""
    if on_stage is None:
        return
    try:
        await on_stage(stage)
    except Exception:  # noqa: BLE001 - progress reporting is not load-bearing
        logger.warning("could not report stage %s", stage, exc_info=True)


def build_graph(
    client: OpenRouterClient, retriever: Retriever | None, on_stage: StageCallback = None
) -> Any:
    """Compile the analysis graph with its dependencies bound in.

    LangGraph nodes take only state, so the client and retriever are closed over
    here rather than smuggled through the state dict -- keeping state to plain
    serialisable data is what lets it be checkpointed and logged.

    `on_stage` fires as each stage begins. The UI shows named progress steps
    rather than a spinner (PRD §7.1), and those names have to come from where
    the pipeline actually is -- a timer pretending to be progress would drift
    from reality the moment a stage ran long.
    """
    graph = StateGraph(AnalysisState)

    async def evidence_node(state: AnalysisState) -> dict:
        await _announce(on_stage, "extracting")
        return await extract_evidence(state, client)

    async def retrieval_node(state: AnalysisState) -> dict:
        await _announce(on_stage, "retrieving")
        return await retrieve_knowledge(state, client, retriever)

    async def synthesis_node(state: AnalysisState) -> dict:
        await _announce(on_stage, "synthesising")
        return await synthesise(state, client)

    graph.add_node("evidence", evidence_node)
    graph.add_node("retrieve", retrieval_node)
    graph.add_node("synthesise", synthesis_node)

    for perspective in Perspective:
        # Bind the perspective per node; a late-binding closure over the loop
        # variable would give all three the same value.
        def make_node(p: Perspective):
            async def node(state: AnalysisState) -> dict:
                # All three fan out together, so the first to start is what the
                # user is waiting on; reporting from each is harmless and avoids
                # a separate co-ordination step.
                await _announce(on_stage, "reasoning")
                return await run_specialist(state, client, p)

            return node

        graph.add_node(perspective.value, make_node(perspective))

    graph.add_edge(START, "evidence")
    graph.add_edge("evidence", "retrieve")
    # Fan out: all three specialists start once retrieval completes.
    for perspective in Perspective:
        graph.add_edge("retrieve", perspective.value)
        graph.add_edge(perspective.value, "synthesise")
    graph.add_edge("synthesise", END)

    return graph.compile()


async def run_analysis(
    *,
    client: OpenRouterClient,
    retriever: Retriever | None,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    tenant_id: str | None = None,
    quality_flags: list[str] | None = None,
    display_type: str | None = None,
    campaign_objective: str | None = None,
    hero_product: str | None = None,
    brand_context: dict | None = None,
    timeout_seconds: int | None = None,
    on_stage: StageCallback = None,
) -> tuple[AnalysisResult, dict]:
    """Run one analysis end to end.

    Returns the result and a telemetry dict -- per-stage timings, model versions
    and measured cost -- which the caller persists against the analysis row.
    """
    settings = get_settings()
    compiled = build_graph(client, retriever, on_stage)

    initial: AnalysisState = {
        "image_bytes": image_bytes,
        "mime_type": mime_type,
        "tenant_id": tenant_id,
        "quality_flags": quality_flags or [],
        "display_type": display_type,
        "campaign_objective": campaign_objective,
        "hero_product": hero_product,
        "brand_context": brand_context,
        "retrieved": {},
        "findings": [],
        "stage_timings_ms": {},
        "model_versions": {},
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "errors": [],
    }

    started = time.monotonic()
    final = await asyncio.wait_for(
        compiled.ainvoke(initial),
        timeout=timeout_seconds or settings.analysis_timeout_seconds,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    result = AnalysisResult(
        evidence=final["evidence"],
        findings=sorted(final.get("findings", []), key=lambda f: f.perspective.value),
        synthesis=final["synthesis"],
        low_confidence=bool(final["evidence"].image_quality_notes),
    )

    telemetry = {
        "stage_timings_ms": {**final.get("stage_timings_ms", {}), "total": elapsed_ms},
        "model_versions": final.get("model_versions", {}),
        "cost_usd": round(final.get("cost_usd", 0.0), 6),
        "prompt_tokens": final.get("prompt_tokens", 0),
        "completion_tokens": final.get("completion_tokens", 0),
        "errors": final.get("errors", []),
        "retrieved_chunk_ids": [
            chunk.id
            for chunks in (final.get("retrieved") or {}).values()
            for chunk in chunks
        ],
    }
    return result, telemetry
