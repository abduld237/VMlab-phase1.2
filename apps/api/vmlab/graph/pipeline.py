"""The analysis graph.

    validate -> extract_evidence -> retrieve -> [3 specialists in parallel]
             -> reconcile -> synthesise

Evidence extraction runs once and feeds all three specialists. That was the
design the client endorsed in his 16 July email, and it is also what keeps the
perspectives genuinely distinct: each specialist sees the same facts but only its
own domain's knowledge, so the three outputs differ in substance rather than
being three rewordings of one answer.

Shared evidence has one cost, and `reconcile` is what pays it. Reasoning from
identical facts, three specialists reach the same conclusion often enough that
the client noticed his report saying one thing three times. The specialists
cannot see each other -- that is what running them in parallel means -- so the
first point in the graph where a duplicate is even visible is after the fan-in.

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
from vmlab.graph.nodes.reconcile import reconcile_findings
from vmlab.graph.priorities import balanced_weights
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

    # The three searches are independent and each is a round trip to a database
    # in another continent, so running them in sequence pays that cost three
    # times for no reason. The pool allows ten connections; three is safe.
    perspectives = list(Perspective)
    results = await asyncio.gather(
        *(
            retriever.search(
                domain=perspective.value,
                embedding=vector,
                tenant_id=state.get("tenant_id"),
                top_k=get_settings().retrieval_top_k,
            )
            for perspective in perspectives
        )
    )
    retrieved: dict[str, list] = {
        perspective.value: chunks
        for perspective, chunks in zip(perspectives, results, strict=True)
    }

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

    async def reconcile_node(state: AnalysisState) -> dict:
        # No stage announcement: this is sub-second and has no model call, so a
        # status the user sees for one frame would read as a flicker. The
        # pipeline is still "reasoning" as far as the UI is concerned.
        return await reconcile_findings(state, client)

    async def synthesis_node(state: AnalysisState) -> dict:
        await _announce(on_stage, "synthesising")
        return await synthesise(state, client)

    graph.add_node("evidence", evidence_node)
    graph.add_node("retrieve", retrieval_node)
    graph.add_node("reconcile", reconcile_node)
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
    # Fan out: all three specialists start once retrieval completes, and fan
    # back in on reconcile rather than straight into synthesis -- LangGraph runs
    # reconcile once, after the last specialist finishes, which is exactly when
    # the full set is first available to compare.
    for perspective in Perspective:
        graph.add_edge("retrieve", perspective.value)
        graph.add_edge(perspective.value, "reconcile")
    graph.add_edge("reconcile", "synthesise")
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
    priorities: dict[str, int] | None = None,
    timeout_seconds: int | None = None,
    on_stage: StageCallback = None,
) -> tuple[AnalysisResult, dict]:
    """Run one analysis end to end.

    Returns the result and a telemetry dict -- per-stage timings, model versions
    and measured cost -- which the caller persists against the analysis row.

    `priorities` is the user's slider mix, keyed by perspective value and
    summing to 100. Omitting it means balanced, which resolves to the depth band
    holding the pipeline's original wording -- so a caller that does not care
    gets the behaviour it had before the mix existed.
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
        "priorities": priorities or balanced_weights(),
        "retrieved": {},
        "findings": [],
        "stage_timings_ms": {},
        "stage_attempts": {},
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

    reconciled = final.get("reconciled") or final.get("findings", [])
    result = AnalysisResult(
        evidence=final["evidence"],
        findings=sorted(reconciled, key=lambda f: f.perspective.value),
        synthesis=final["synthesis"],
        low_confidence=bool(final["evidence"].image_quality_notes),
    )

    telemetry = {
        "stage_timings_ms": {**final.get("stage_timings_ms", {}), "total": elapsed_ms},
        "stage_attempts": final.get("stage_attempts", {}),
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
        # Keyed by perspective as well as flattened, because each specialist's
        # section records the chunks that specialist actually saw. The persist
        # layer reads this key; without it the audit columns were written empty
        # on every analysis, and the whole point of them is being able to prove
        # after the fact which knowledge a tenant's result was built from.
        "evidence_refs": {
            domain: [chunk.id for chunk in chunks]
            for domain, chunks in (final.get("retrieved") or {}).items()
        },
    }
    return result, telemetry
