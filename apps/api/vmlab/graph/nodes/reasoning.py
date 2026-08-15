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
from collections.abc import Callable
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

# Rule-bearing chunks average 2,000-2,500 characters, so a 6,000 budget fit only
# two or three and routinely cut every citable rule out of the prompt. This holds
# roughly the full top-k without meaningfully moving cost: a measured analysis
# runs at $0.0024 and the prompt is a small part of that.
MAX_KNOWLEDGE_CHARS = 16000


class SpecialistItems(BaseModel):
    """The {"items": [...]} envelope the specialist prompt asks for.

    Models are markedly more reliable returning a named array inside an object
    than a bare top-level array, so the envelope is asked for and unwrapped here.
    """

    items: list[SpecialistItem]


# Keys a model plausibly uses for the items array when it ignores the envelope.
_ITEMS_ALIASES = ("items", "findings", "observations", "recommendations", "results")


def _coerce_items(payload: Any) -> Any:
    """Reshape near-miss specialist responses into the {"items": [...]} envelope.

    A reasoning model that narrates before answering returns things like
    {"analysis": "We need to ..."} or a bare top-level array. Both carry the
    content we asked for in a shape pydantic rejects. Repairing the envelope
    here costs nothing and avoids burning a retry -- and a burnt retry is how
    the creative_vm specialist dropped out of a real run entirely.
    """
    if isinstance(payload, list):
        return {"items": payload}
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("items"), list):
        return payload

    for alias in _ITEMS_ALIASES:
        if isinstance(payload.get(alias), list):
            return {"items": payload[alias]}

    # Last resort: exactly one list-of-objects value, whatever it is called.
    lists = [
        value
        for value in payload.values()
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
    ]
    if len(lists) == 1:
        return {"items": lists[0]}
    return payload


def _top_level_keys(schema: type) -> str:
    """The schema's own field names, for the retry instruction.

    Derived rather than written out: an earlier version hardcoded the
    specialist's keys into this shared helper, so a synthesis retry told the
    model to return specialist output -- and it obliged, failing all three
    attempts on a schema it had been instructed to violate.
    """
    fields = getattr(schema, "model_fields", None)
    if not fields:
        return "as described above"
    return ", ".join(f'"{name}"' for name in fields)


async def _call_json(
    client: OpenRouterClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    schema: type,
    max_tokens: int = 2500,
    attempts: int = 3,
    coerce: Callable[[Any], Any] | None = None,
    retry_reminder: str = "",
) -> tuple[Any, Completion]:
    """Call the model and validate the response, retrying on bad shape.

    `coerce` gets a chance to repair a near-miss payload before validation, so
    a recoverable shape error does not consume an attempt.
    """
    last_error: Exception | None = None
    base_messages = messages

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
            payload = completion.as_json()
            if coerce is not None:
                payload = coerce(payload)
            return schema.model_validate(payload), completion
        except (ValidationError, OpenRouterError) as exc:
            last_error = exc
            logger.warning("schema validation failed on attempt %d: %s", attempt + 1, exc)
            # Rebuild from the original messages rather than appending each
            # failure. Feeding a reasoning model its own narration back is what
            # makes it narrate again, and the transcript grows every round.
            messages = base_messages + [
                {"role": "assistant", "content": completion.text[:600]},
                {
                    "role": "user",
                    "content": (
                        f"That response did not match the required schema: "
                        f"{str(exc)[:400]}. Respond with a single JSON object and "
                        f"nothing else -- no prose, no explanation, no reasoning. "
                        f"Its top-level keys must be exactly: "
                        f"{_top_level_keys(schema)}." + retry_reminder
                    ),
                },
            ]

    raise OpenRouterError(f"model failed schema validation after {attempts} attempts: {last_error}")


def _permitted_rule_ids(chunks: list[RetrievedChunk]) -> set[str]:
    """Every rule ID actually present in what was retrieved for this perspective."""
    return {rule_id for chunk in chunks for rule_id in chunk.rule_ids}


def _enforce_citations(items: list[SpecialistItem], permitted: set[str], label: str) -> None:
    """Drop cited rule IDs that are not in the retrieved evidence.

    Left unchecked the model invents citations that look exactly like the real
    scheme -- a run against a real photo produced V20-10.1, V16-19.3, V16-12.1
    and V19-18.1, none of which exist anywhere in the corpus. A fabricated
    citation is worse than no citation: it reads as authoritative and cannot be
    checked by the person reading the report. Only IDs the retrieval layer
    actually returned may be shown, so a claim can always be traced back to a
    real chunk of the client's own standards.
    """
    for item in items:
        kept = [rule_id for rule_id in item.supporting_rule_ids if rule_id in permitted]
        dropped = [rule_id for rule_id in item.supporting_rule_ids if rule_id not in permitted]
        if dropped:
            logger.warning("%s cited rule ids not in retrieved evidence: %s", label, dropped)
        item.supporting_rule_ids = kept


def _render_knowledge(chunks: list[RetrievedChunk]) -> tuple[str, list[RetrievedChunk]]:
    """Format retrieved chunks for a prompt, cheapest-to-drop last.

    Chunks arrive authority-first, so truncating from the end drops illustrative
    material before canonical -- the opposite order would silently discard the
    client's own standards when a bundle runs long.

    Returns the rendered text and the chunks that actually fit. Callers need the
    second value: a rule whose text was truncated away cannot honestly be cited,
    so the permitted-citation set has to follow what the model was shown rather
    than everything retrieval returned.
    """
    if not chunks:
        return "(no knowledge base excerpts were retrieved for this perspective)", []

    # Within an authority tier, a chunk carrying rule ids goes first. Doc 29 §13
    # is explicit that the governing rule matters more than the closest match,
    # and a chunk with no rule id cannot be cited at all -- so when space is
    # short, the citable one earns the room. Authority order itself is never
    # reordered; illustrative material must not overtake canonical.
    order = {"canonical": 0, "guidance": 1, "illustrative": 2, "historical": 3}
    ranked = sorted(
        enumerate(chunks),
        key=lambda pair: (
            order.get(pair[1].authority, 9),
            0 if pair[1].rule_ids else 1,
            pair[0],  # keep retrieval's ranking as the final tiebreak
        ),
    )

    rendered: list[str] = []
    used: list[RetrievedChunk] = []
    budget = MAX_KNOWLEDGE_CHARS
    for _, chunk in ranked:
        rules = f" [rules: {', '.join(chunk.rule_ids)}]" if chunk.rule_ids else ""
        block = f"--- {chunk.citation} ({chunk.authority}){rules}\n{chunk.content}"
        # Skip rather than stop: one oversized chunk should not shut out every
        # smaller one behind it.
        if len(block) > budget:
            continue
        rendered.append(block)
        used.append(chunk)
        budget -= len(block)
    return "\n\n".join(rendered), used


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

    # Naming the legal identifiers inline is the cheap half of the citation
    # guard; _enforce_citations below is the half that actually holds.
    #
    # Permitted ids come from the chunks that survived truncation, not from
    # everything retrieved -- offering an id whose rule text was cut leaves the
    # model citing something it cannot see.
    knowledge, shown = _render_knowledge(chunks)
    permitted = _permitted_rule_ids(shown)
    if permitted:
        # Phrased as an expectation, not just a prohibition. An earlier version
        # ended on "if no listed rule supports a point, return an empty array"
        # and the model took that exit every time, trading fabricated citations
        # for no citations at all.
        citation_rule = (
            "CITATIONS: cite the rule identifiers that support your points. "
            "Valid identifiers, copied exactly: " + ", ".join(sorted(permitted))
            + ". Most points should carry at least one. Never invent an "
            "identifier, never reformat one, and never cite a document id such "
            "as VMLAB-KB-14A.3 -- only the rule ids listed above are valid."
        )
    else:
        citation_rule = (
            "CITATIONS: no rule identifiers were retrieved for this perspective, "
            "so supporting_rule_ids must be an empty array on every item."
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
                knowledge=knowledge,
                context=context,
            )
            + "\n\n"
            + citation_rule,
        },
    ]

    try:
        parsed, completion = await _call_json(
            client,
            model=settings.reasoning_model,
            messages=messages,
            schema=SpecialistItems,
            coerce=_coerce_items,
            # The retry message becomes the last thing the model reads, so the
            # citation rule has to travel with it or a retried specialist
            # silently returns no citations at all.
            retry_reminder=" " + citation_rule,
        )
    except OpenRouterError as exc:
        # Degrade: two perspectives plus an honest note beats no result at all.
        logger.error("specialist %s failed: %s", perspective.value, exc)
        return {
            "errors": [f"{perspective.value} specialist did not complete: {exc}"],
            "stage_timings_ms": {perspective.value: int((time.monotonic() - started) * 1000)},
        }

    _enforce_citations(parsed.items, permitted, perspective.value)
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

    # Synthesis may only carry citations forward, never introduce new ones: the
    # specialists' ids are already filtered against retrieved evidence, so
    # anything outside that union was invented at this stage.
    carried = {
        rule_id
        for finding in findings
        for item in finding.items
        for rule_id in item.supporting_rule_ids
    }
    for action in parsed.actions:
        dropped = [r for r in action.supporting_rule_ids if r not in carried]
        if dropped:
            logger.warning("synthesis cited rule ids no specialist supported: %s", dropped)
        action.supporting_rule_ids = [r for r in action.supporting_rule_ids if r in carried]

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

