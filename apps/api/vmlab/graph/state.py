"""The state threaded through the analysis graph.

The three specialists run in parallel and all write to the same `findings` key,
so it needs a reducer -- without one, LangGraph raises on the concurrent update
and the fan-out silently becomes a fan-in of one. `operator.add` on a list is
the whole mechanism.

Costs and timings accumulate the same way, which is what makes the per-analysis
figure reported to the client a measurement rather than an estimate.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from vmlab.graph.schemas import SpecialistFinding, Synthesis, VisualEvidence
from vmlab.retrieval.retriever import RetrievedChunk


def merge_dicts(left: dict, right: dict) -> dict:
    return {**left, **right}


class AnalysisState(TypedDict, total=False):
    # --- inputs ------------------------------------------------------------
    analysis_id: str
    tenant_id: str
    image_bytes: bytes
    mime_type: str
    quality_flags: list[str]
    # Optional context the user supplied before submitting (FR-04).
    display_type: str | None
    campaign_objective: str | None
    hero_product: str | None
    # The active tenant's brand profile. Only ever this tenant's (FR-20).
    brand_context: dict | None
    # How the user weighted the three specialists for this run, keyed by
    # perspective value and summing to 100. Set once before the graph starts and
    # read by the specialists and the reconcile stage; never written by a node,
    # so it needs no reducer.
    priorities: dict[str, int]

    # --- stage outputs -----------------------------------------------------
    evidence: VisualEvidence
    retrieved: Annotated[dict[str, list[RetrievedChunk]], merge_dicts]
    findings: Annotated[list[SpecialistFinding], operator.add]
    # The same findings with cross-perspective duplicates removed. A separate
    # key rather than a rewrite of `findings`: that one accumulates through
    # `operator.add`, so a node returning a shorter list would append it instead
    # of replacing it. Written by one node only, hence no reducer here either.
    reconciled: list[SpecialistFinding]
    synthesis: Synthesis

    # --- observability -----------------------------------------------------
    stage_timings_ms: Annotated[dict[str, int], merge_dicts]
    # How many model calls each stage actually took. A stage that needed three
    # attempts paid three times over in latency, and without this the only
    # symptom is a slow run with no explanation.
    stage_attempts: Annotated[dict[str, int], merge_dicts]
    model_versions: Annotated[dict[str, str], merge_dicts]
    cost_usd: Annotated[float, operator.add]
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]
    errors: Annotated[list[str], operator.add]
