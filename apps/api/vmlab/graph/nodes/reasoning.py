"""The model-driven stages: evidence extraction, the specialists, and synthesis.

Every node here follows the same discipline. Call the model asking for JSON,
validate against the pydantic schema, retry once on a schema failure, and on a
second failure degrade rather than abort -- a partial analysis with two
specialists is worth more to the user than an error page, and the missing
perspective is recorded in state["errors"] so it surfaces honestly rather than
looking like a complete result.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from vmlab.config import get_settings
from vmlab.graph import prompts
from vmlab.graph.schemas import (
    Perspective,
    SpecialistFinding,
    SpecialistItem,
    Synthesis,
    VisualEvidence,
)
from vmlab.graph.state import AnalysisState
from vmlab.models.openrouter import Completion, OpenRouterClient, OpenRouterError
from vmlab.retrieval.retriever import RetrievedChunk

logger = logging.getLogger(__name__)

MAX_KNOWLEDGE_CHARS = 6000


class SpecialistItems(BaseModel):
    """The {"items": [...]} envelope the specialist prompt asks for.

    Models are markedly more reliable returning a named array inside an object
    than a bare top-level array, so the envelope is asked for and unwrapped here.
    """

    items: list[SpecialistItem]


async def _call_json(
    client: OpenRouterClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    schema: type,
    max_tokens: int = 2500,
    attempts: int = 2,
) -> tuple[Any, Completion]:
    """Call the model and validate the response, retrying once on bad shape."""
    last_error: Exception | None = None

    for attempt in range(attempts):
        completion = await client.complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            json_object=True,
            # A retry at the same temperature tends to reproduce the same
            # malformed output, so nudge it up.
            temperature=0.2 if attempt == 0 else 0.5,
        )
        try:
            return schema.model_validate(completion.as_json()), completion
        except (ValidationError, OpenRouterError) as exc:
            last_error = exc
            logger.warning("schema validation failed on attempt %d: %s", attempt + 1, exc)
            messages = messages + [
                {"role": "assistant", "content": completion.text[:1500]},
                {
                    "role": "user",
                    "content": (
                        f"That response did not match the required schema: {exc}. "
                        f"Return only valid JSON matching the schema exactly."
                    ),
                },
            ]

    raise OpenRouterError(f"model failed schema validation after {attempts} attempts: {last_error}")


def _render_knowledge(chunks: list[RetrievedChunk]) -> str:
    """Format retrieved chunks for a prompt, cheapest-to-drop last.

    Chunks arrive authority-first, so truncating from the end drops illustrative
    material before canonical -- the opposite order would silently discard the
    client's own standards when a bundle runs long.
    """
    if not chunks:
        return "(no knowledge base excerpts were retrieved for this perspective)"

    rendered: list[str] = []
    budget = MAX_KNOWLEDGE_CHARS
    for chunk in chunks:
        rules = f" [rules: {', '.join(chunk.rule_ids)}]" if chunk.rule_ids else ""
        block = f"--- {chunk.citation} ({chunk.authority}){rules}\n{chunk.content}"
        if len(block) > budget:
            break
        rendered.append(block)
        budget -= len(block)
    return "\n\n".join(rendered)


# ---------------------------------------------------------------------------


async def extract_evidence(state: AnalysisState, client: OpenRouterClient) -> dict:
    """Stage 1: one vision call producing the Document 25 shaped scene record."""
    settings = get_settings()
    started = time.monotonic()

    context = prompts.format_context(
        state.get("display_type"),
        state.get("campaign_objective"),
        state.get("hero_product"),
        state.get("brand_context"),
    )

    encoded = base64.b64encode(state["image_bytes"]).decode("ascii")
    mime_type = state.get("mime_type", "image/jpeg")
    messages = [
        {"role": "system", "content": prompts.EVIDENCE_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompts.EVIDENCE_USER.format(context=context)},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                },
            ],
        },
    ]

    # Evidence goes through the same retrying validator as the specialists.
    # It cannot degrade -- there is nothing for the specialists to reason over
    # without it -- so a single malformed or truncated response must not be
    # fatal. The budget is deliberately generous: too low truncates the JSON
    # mid-object, which fails validation for a reason no retry can fix.
    try:
        evidence, completion = await _call_json(
            client,
            model=settings.vision_model,
            messages=messages,
            schema=VisualEvidence,
            max_tokens=2600,
        )
    except (ValidationError, OpenRouterError) as exc:
        raise OpenRouterError(f"evidence extraction failed: {exc}") from exc

    # Quality flags from validation belong with the model's own observations, so
    # the synthesiser sees the full picture when deciding on an uncertainty note.
    evidence.image_quality_notes = list(
        dict.fromkeys([*state.get("quality_flags", []), *evidence.image_quality_notes])
    )

    return {
        "evidence": evidence,
        "stage_timings_ms": {"evidence": int((time.monotonic() - started) * 1000)},
        "model_versions": {"vision": completion.model},
        "cost_usd": completion.cost_usd,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
    }


async def run_specialist(
    state: AnalysisState, client: OpenRouterClient, perspective: Perspective
) -> dict:
    """Stage 3: one specialist perspective over shared evidence and its own domain."""
    settings = get_settings()
    started = time.monotonic()

    chunks = (state.get("retrieved") or {}).get(perspective.value, [])
    context = prompts.format_context(
        state.get("display_type"),
        state.get("campaign_objective"),
        state.get("hero_product"),
        state.get("brand_context"),
    )

    messages = [
        {
            "role": "system",
            "content": prompts.SPECIALIST_SYSTEM.format(
                brief=prompts.SPECIALIST_BRIEFS[perspective]
            ),
        },
        {
            "role": "user",
            "content": prompts.SPECIALIST_USER.format(
                evidence=state["evidence"].model_dump_json(indent=2),
                knowledge=_render_knowledge(chunks),
                context=context,
            ),
        },
    ]

    try:
        parsed, completion = await _call_json(
            client,
            model=settings.reasoning_model,
            messages=messages,
            schema=SpecialistItems,
        )
    except OpenRouterError as exc:
        # Degrade: two perspectives plus an honest note beats no result at all.
        logger.error("specialist %s failed: %s", perspective.value, exc)
        return {
            "errors": [f"{perspective.value} specialist did not complete: {exc}"],
            "stage_timings_ms": {perspective.value: int((time.monotonic() - started) * 1000)},
        }

    finding = SpecialistFinding(perspective=perspective, items=parsed.items)
    return {
        "findings": [finding],
        "stage_timings_ms": {perspective.value: int((time.monotonic() - started) * 1000)},
        "model_versions": {perspective.value: completion.model},
        "cost_usd": completion.cost_usd,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
    }


async def synthesise(state: AnalysisState, client: OpenRouterClient) -> dict:
    """Stage 4: merge the perspectives into the ranked top three (FR-10)."""
    settings = get_settings()
    started = time.monotonic()

    findings = state.get("findings", [])
    if not findings:
        raise OpenRouterError("no specialist findings to synthesise")

    rendered = "\n\n".join(
        f"### {finding.perspective.value}\n{finding.model_dump_json(indent=2)}"
        for finding in findings
    )
    quality = state["evidence"].image_quality_notes or ["none"]

    messages = [
        {"role": "system", "content": prompts.SYNTHESIS_SYSTEM},
        {
            "role": "user",
            "content": prompts.SYNTHESIS_USER.format(
                findings=rendered, quality="; ".join(quality)
            ),
        },
    ]

    parsed, completion = await _call_json(
        client,
        model=settings.reasoning_model,
        messages=messages,
        schema=Synthesis,
        max_tokens=3000,
    )

    # If a specialist dropped out, disclose it here rather than letting the
    # result read as a complete three-perspective analysis.
    missing = {p.value for p in Perspective} - {f.perspective.value for f in findings}
    if missing:
        note = f"This analysis is missing the {', '.join(sorted(missing))} perspective."
        parsed.uncertainty_note = f"{parsed.uncertainty_note} {note}".strip() if parsed.uncertainty_note else note

    return {
        "synthesis": parsed,
        "stage_timings_ms": {"synthesis": int((time.monotonic() - started) * 1000)},
        "model_versions": {"synthesis": completion.model},
        "cost_usd": completion.cost_usd,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
    }

