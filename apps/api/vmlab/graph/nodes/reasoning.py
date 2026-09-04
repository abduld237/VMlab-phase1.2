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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model

from vmlab.config import get_settings
from vmlab.graph import prompts
from vmlab.graph.priorities import Band, balanced_weights, band_for, lead_perspective
from vmlab.graph.schemas import (
    Perspective,
    SpecialistFinding,
    SpecialistItem,
    SpecialistItemDraft,
    Synthesis,
    VisualEvidence,
)
from vmlab.graph.state import AnalysisState
from vmlab.models.openrouter import (
    Completion,
    OpenRouterClient,
    OpenRouterError,
    strict_schema,
)
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

    This is the balanced-mix shape. A weighted analysis uses `items_envelope`
    below, which is the same model with the bounds moved.
    """

    # FR-07/08/09 require three to five. Asking pydantic to enforce the floor is
    # what turns a thin answer into a retry instead of into a section the client
    # can see is short. Retries cost about four seconds now, so the floor is
    # affordable in a way it was not when a specialist took seventy.
    items: list[SpecialistItemDraft] = Field(min_length=3)


@lru_cache(maxsize=16)
def items_envelope(min_items: int, max_items: int) -> type[BaseModel]:
    """The envelope model for one priority band.

    Built per band rather than validated afterwards so the bounds reach the
    provider: `_schema_for` turns this into the strict JSON schema the decoder
    enforces, which means a muted specialist is *prevented* from returning five
    items rather than having two of them trimmed off after we paid for them.

    Cached because `_schema_for` keys its own cache on the type object -- a
    fresh class per call would rebuild the schema on every analysis and never
    hit either cache.
    """
    return create_model(
        f"SpecialistItems{min_items}To{max_items}",
        items=(list[SpecialistItemDraft], Field(min_length=min_items, max_length=max_items)),
    )


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


@dataclass
class CallOutcome:
    """What a validated call cost in total, not just on the attempt that worked.

    An earlier version returned only the successful `Completion`, so every retry
    a stage burned was invisible: its tokens and its cost vanished from the
    per-analysis figure reported to the client, and a stage that quietly cost
    three calls looked identical to one that cost a single call. Since a retry is
    a whole duplicate request, that is also where the latency hides.
    """

    completion: Completion | None
    attempts: int = 0
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, completion: Completion) -> None:
        self.completion = completion
        self.attempts += 1
        self.cost_usd += completion.cost_usd
        self.prompt_tokens += completion.prompt_tokens
        self.completion_tokens += completion.completion_tokens

    def telemetry(self, stage: str) -> dict[str, Any]:
        """The observability half of a node's return value."""
        model = self.completion.model if self.completion else "unknown"
        provider = self.completion.provider if self.completion else ""
        return {
            # "model via Provider" rather than the bare model id: one id is
            # served by many upstreams at wildly different speeds, so the
            # provider is the part that explains a slow run.
            "model_versions": {stage: f"{model} via {provider}" if provider else model},
            "stage_attempts": {stage: self.attempts},
            "cost_usd": self.cost_usd,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class SchemaCallFailed(OpenRouterError):
    """Validation failed on every attempt, carrying what those attempts cost."""

    def __init__(self, message: str, outcome: CallOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def _reasoning_for(effort: str) -> dict[str, Any] | None:
    """Translate the configured effort into OpenRouter's reasoning parameter.

    An empty setting means "leave the model at its own default", which is not
    the same as low -- so it sends nothing rather than a value.
    """
    return {"effort": effort} if effort else None


@lru_cache(maxsize=8)
def _schema_for(schema: type) -> dict[str, Any] | None:
    """The strict structured-output schema for a pydantic model, built once.

    Returns None when strict schemas are switched off, which falls the caller
    back to asking for a JSON object and validating afterwards.
    """
    if not get_settings().use_strict_schemas:
        return None
    return strict_schema(schema.model_json_schema())


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
    reasoning: dict[str, Any] | None = None,
    strict: bool = True,
) -> tuple[Any, CallOutcome]:
    """Call the model and validate the response, retrying on bad shape.

    `coerce` gets a chance to repair a near-miss payload before validation, so
    a recoverable shape error does not consume an attempt.

    With strict schemas enabled the provider's decoder guarantees the shape and
    the retry loop should never run. It stays because the guarantee covers
    structure, not semantics: pydantic still enforces the bounds and the
    `Observed only` rule that strict mode cannot express.
    """
    last_error: Exception | None = None
    base_messages = messages
    outcome = CallOutcome(completion=None)
    json_schema = _schema_for(schema) if strict else None

    for attempt in range(attempts):
        completion = await client.complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            json_object=True,
            json_schema=json_schema,
            schema_name=schema.__name__.lower(),
            reasoning=reasoning,
            # A retry at the same temperature tends to reproduce the same
            # malformed output, so nudge it up.
            temperature=0.2 if attempt == 0 else 0.5,
        )
        outcome.record(completion)
        try:
            payload = completion.as_json()
            if coerce is not None:
                payload = coerce(payload)
            return schema.model_validate(payload), outcome
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

    raise SchemaCallFailed(
        f"model failed schema validation after {attempts} attempts: {last_error}", outcome
    )


def _salvage_items(outcome: CallOutcome | None, max_items: int = 5) -> list[SpecialistItem]:
    """Recover whatever valid items the last failed attempt did contain.

    The envelope demands a floor of items, so a specialist that can only find
    one below it fails validation on every attempt and would otherwise be
    dropped entirely. Two grounded findings are worth more to the reader than a
    missing perspective, and far more than a retry loop that ends in nothing --
    so the floor drives a retry, and this is what happens when the retry does
    not help.

    The cap follows the band rather than a fixed five: an over-long response
    from a muted specialist is trimmed to what the user actually asked for.
    """
    if outcome is None or outcome.completion is None:
        return []
    try:
        payload = _coerce_items(outcome.completion.as_json())
    except OpenRouterError:
        return []
    entries = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    salvaged: list[SpecialistItem] = []
    for entry in entries:
        try:
            salvaged.append(SpecialistItem.model_validate(entry))
        except ValidationError:
            continue
    return salvaged[:max_items]


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
        evidence, outcome = await _call_json(
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
        **outcome.telemetry("vision"),
    }


def _band_and_ownership(state: AnalysisState, perspective: Perspective) -> tuple[Band, str]:
    """This specialist's depth band, and whether it leads the review.

    Weights are absent for anything started before the priority mix existed and
    for direct callers that do not care, so they fall back to balanced -- which
    resolves to the band carrying the pipeline's original wording verbatim.
    """
    weights = state.get("priorities") or balanced_weights()
    band = band_for(weights.get(perspective.value, 0))
    leads = lead_perspective(weights) is perspective
    return band, prompts.LEAD_PERSPECTIVE_RULE if leads else ""


async def run_specialist(
    state: AnalysisState, client: OpenRouterClient, perspective: Perspective
) -> dict:
    """Stage 3: one specialist perspective over shared evidence and its own domain."""
    settings = get_settings()
    started = time.monotonic()

    band, ownership = _band_and_ownership(state, perspective)
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
                brief=prompts.SPECIALIST_BRIEFS[perspective],
                depth=band.depth,
                ownership=ownership,
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
        parsed, outcome = await _call_json(
            client,
            model=settings.reasoning_model,
            messages=messages,
            schema=items_envelope(band.min_items, band.max_items),
            coerce=_coerce_items,
            reasoning=_reasoning_for(settings.reasoning_effort),
            # The retry message becomes the last thing the model reads, so the
            # citation rule has to travel with it or a retried specialist
            # silently returns no citations at all.
            retry_reminder=" " + citation_rule,
        )
    except OpenRouterError as exc:
        logger.error("specialist %s failed: %s", perspective.value, exc)
        # A failed specialist still spent whatever its attempts cost. Reporting
        # zero there would understate the analysis by up to three calls.
        spent = getattr(exc, "outcome", None)
        timing = {perspective.value: int((time.monotonic() - started) * 1000)}
        telemetry = spent.telemetry(perspective.value) if spent else {}

        # Short of the three-item floor is not the same failure as unusable
        # output. Keep what was grounded and record that it came up short.
        salvaged = _salvage_items(spent, band.max_items)
        if salvaged:
            _enforce_citations(salvaged, permitted, perspective.value)
            return {
                "findings": [SpecialistFinding(perspective=perspective, items=salvaged)],
                "errors": [
                    f"{perspective.value} returned only {len(salvaged)} "
                    f"{'item' if len(salvaged) == 1 else 'items'} against a floor of "
                    f"{band.min_items}"
                ],
                "stage_timings_ms": timing,
                **telemetry,
            }

        # Degrade: two perspectives plus an honest note beats no result at all.
        return {
            "errors": [f"{perspective.value} specialist did not complete: {exc}"],
            "stage_timings_ms": timing,
            **telemetry,
        }

    # Drafts are the model's shape; SpecialistItem is ours. The lift happens
    # here so nothing downstream has to know the difference.
    items = [SpecialistItem.model_validate(draft.model_dump()) for draft in parsed.items]
    _enforce_citations(items, permitted, perspective.value)
    finding = SpecialistFinding(perspective=perspective, items=items)
    return {
        "findings": [finding],
        "stage_timings_ms": {perspective.value: int((time.monotonic() - started) * 1000)},
        **outcome.telemetry(perspective.value),
    }


async def synthesise(state: AnalysisState, client: OpenRouterClient) -> dict:
    """Stage 4: merge the perspectives into the ranked top three (FR-10)."""
    settings = get_settings()
    started = time.monotonic()

    # Prefer the reconciled set: a point the reconcile stage removed as a
    # duplicate must not come back here and be counted as two perspectives
    # agreeing. `findings` is the fallback for a reconcile that degraded.
    findings = state.get("reconciled") or state.get("findings", [])
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

    parsed, outcome = await _call_json(
        client,
        model=settings.reasoning_model,
        messages=messages,
        schema=Synthesis,
        max_tokens=3000,
        reasoning=_reasoning_for(settings.reasoning_effort),
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
        **outcome.telemetry("synthesis"),
    }

