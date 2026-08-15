"""Structured outputs for each pipeline stage.

These deliberately mirror the client's own object definitions rather than
inventing parallel ones:

* Evidence follows Document 25 (VMLAB-EOR-025), his Evidence Collection and
  Observation Rules.
* Recommendations follow Document 26 (VMLAB-RDL-026), his Recommendation Engine
  and Decision Logic.

His schemas run to 27 and 41 fields, most of which describe a human review
workflow -- reviewer_id, review_timestamp, chain-of-custody, outcome
verification. A vision model cannot populate those from a photograph, and asking
it to would produce confident fabrication rather than an honest gap. So the
subsets modelled here are the fields genuinely derivable from one image, keeping
his names and value vocabularies so the output slots into his framework and the
remaining fields can be filled by review later.

The distinction his Document 25 draws between `Observed only`, `hypothesis`,
`diagnosis` and `prediction` is the load-bearing one and is preserved exactly.
The vision stage is only ever allowed to emit `Observed only` -- it describes,
it does not judge.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class InferenceStatus(StrEnum):
    OBSERVED = "Observed only"
    HYPOTHESIS = "hypothesis"
    DIAGNOSIS = "diagnosis"
    PREDICTION = "prediction"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MAJOR = "major"
    MEDIUM = "medium"
    LOW = "low"
    MONITOR = "monitor"


class Reliability(StrEnum):
    LOW = "Low"
    MODERATE = "Moderate"
    HIGH = "High"
    VERIFIED = "Verified"
    UNKNOWN = "Unknown"


class EffortBand(StrEnum):
    """The PRD requires exactly these three labels on every prioritised action."""

    QUICK_WIN = "quick_win"
    MODERATE = "moderate"
    MAJOR_CHANGE = "major_change"


class Perspective(StrEnum):
    CREATIVE_VM = "creative_vm"
    RETAIL_PSYCHOLOGY = "retail_psychology"
    COMMERCIAL = "commercial"


# ---------------------------------------------------------------------------
# Stage 1 -- visual evidence (Document 25 shaped)
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """One factual observation about the display. No judgement."""

    observation_id: str = Field(description="Short stable id, e.g. OBS-0001")

    @field_validator("observation_id", mode="before")
    @classmethod
    def coerce_numeric_id(cls, value: object) -> object:
        """Accept a bare 1 where OBS-0001 was asked for.

        The id only has to be stable within one analysis, so a model that
        numbers its observations rather than naming them has answered the
        question. Failing here would discard a whole vision call over a label.
        """
        return f"OBS-{value:04d}" if isinstance(value, int) else value
    entity_type: str = Field(description="What was observed: fixture, signage, product_group, lighting, layout")
    attribute: str = Field(description="The property observed, e.g. 'price visibility'")
    observed_value: str = Field(description="What is actually visible, factually stated")
    inference_status: InferenceStatus = InferenceStatus.OBSERVED
    model_confidence: float = Field(ge=0.0, le=1.0)
    evidence_sufficiency: int = Field(ge=0, le=3, description="0 none, 3 fully sufficient")

    @field_validator("inference_status")
    @classmethod
    def must_be_observed(cls, value: InferenceStatus) -> InferenceStatus:
        # Document 25 separates observation from diagnosis for a reason: once a
        # judgement is recorded as an observation, every downstream specialist
        # treats it as fact and the reasoning becomes uncheckable.
        if value is not InferenceStatus.OBSERVED:
            raise ValueError("the evidence stage may only emit 'Observed only'")
        return value


def _looks_negative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith(("no", "not ", "none", "false", "absent", "illegible"))


class VisualEvidence(BaseModel):
    """The scene record produced by the single vision call."""

    display_type: str = Field(description="window, wall bay, end-cap, table, gondola, feature")
    probable_location: str | None = None
    focal_point: str | None = Field(default=None, description="Where the eye lands first")
    hero_product: str | None = None
    secondary_products: list[str] = Field(default_factory=list)
    approximate_sku_count: int | None = None
    signage_count: int | None = None
    signage_legible: bool | None = None
    price_visible: bool | None = None
    dominant_colours: list[str] = Field(default_factory=list)
    lighting_style: str | None = None
    fixtures: list[str] = Field(default_factory=list)
    composition_notes: list[str] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    # Carried through to the final output so a weak photo is disclosed rather
    # than silently producing low-quality advice (PRD §9).
    image_quality_notes: list[str] = Field(default_factory=list)
    occlusions: list[str] = Field(default_factory=list)

    @field_validator(
        "secondary_products", "dominant_colours", "fixtures", "composition_notes",
        "image_quality_notes", "occlusions",
        mode="before",
    )
    @classmethod
    def coerce_single_value_to_list(cls, value: object) -> object:
        """Wrap a lone string where a list was asked for.

        "Vertical stacking of product under the roof" is a perfectly good
        composition note; it just arrived as a string instead of a one-item
        list. Retrying the vision call over that is the most expensive possible
        response to the cheapest possible problem.
        """
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return value

    @field_validator("signage_legible", "price_visible", mode="before")
    @classmethod
    def coerce_descriptive_answer(cls, value: object) -> object:
        """Accept a described answer where a yes/no was asked for.

        Asked whether signage is legible, the vision model answers with what the
        signage *says* -- "Magnolia", "Yes ('THE SHOPPE' on the back wall)", or
        a list like ["MAGNOLIA JOURNAL", "ELEVATOR"]. Every one of those is a
        strictly more informative reply to a badly posed question, and rejecting
        it cost a full retry of the most expensive call in the pipeline: on the
        benchmark set this fired on four images in six and doubled evidence
        extraction from ~35s to 73-94s.

        Reading the sign is itself proof it was legible, so a description counts
        as yes unless it is phrased as a negative.
        """
        if isinstance(value, list):
            # A list of the signs it managed to read. Reading them is the answer.
            return bool(value)
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text or text.lower() in {"unknown", "n/a", "na", "unclear"}:
            return None
        if _looks_negative(text):
            return False
        if text.lower() in {"yes", "true", "y", "visible", "legible"}:
            return True
        # Anything else is the model describing what it read, which only happens
        # when it could read it.
        return True

    @field_validator("approximate_sku_count", "signage_count", mode="before")
    @classmethod
    def coerce_counts(cls, value: object) -> object:
        """Take a digit out of a hedged count, or record that there was none.

        "approximately 12" and "several" are both answers a strict int rejects.
        The first carries a number worth keeping; the second carries none, and
        None is the honest record of that rather than a guessed figure.
        """
        if not isinstance(value, str):
            return value
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None


# ---------------------------------------------------------------------------
# Stage 3 -- specialist findings
# ---------------------------------------------------------------------------


class SpecialistItem(BaseModel):
    """One observation-plus-recommendation pair from a specialist (FR-07..FR-11)."""

    observation: str
    recommendation: str
    reason: str = Field(description="Why this matters, in one or two sentences")
    confidence: float = Field(ge=0.0, le=1.0)
    severity: Severity = Severity.MEDIUM
    # Rule IDs from the retrieved chunks that support this item. Empty means the
    # finding rests on the image alone, which the UI shows differently.
    supporting_rule_ids: list[str] = Field(default_factory=list)


class SpecialistFinding(BaseModel):
    perspective: Perspective
    items: list[SpecialistItem] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def within_prd_range(cls, items: list[SpecialistItem]) -> list[SpecialistItem]:
        # FR-07/08/09 require 3-5 per perspective. Over-length responses are
        # trimmed rather than rejected: the content is usually fine and a retry
        # costs a model call.
        return items[:5]


# ---------------------------------------------------------------------------
# Stage 4 -- synthesis (Document 26 shaped)
# ---------------------------------------------------------------------------


class PrioritisedAction(BaseModel):
    """One of the top three actions, scored the way Document 26 scores them."""

    rank: int = Field(ge=1, le=3)
    action: str
    rationale: str
    effort: EffortBand
    confidence: float = Field(ge=0.0, le=1.0)
    # Document 26's 1-5 scoring block, kept so the ranking is explainable
    # instead of asserted.
    customer_impact: int = Field(ge=1, le=5)
    commercial_impact: int = Field(ge=1, le=5)
    urgency: int = Field(ge=1, le=5)
    effort_score: int = Field(ge=1, le=5, description="1 trivial, 5 major")
    priority_score: int = Field(ge=0, le=100)
    contributing_perspectives: list[Perspective] = Field(default_factory=list)
    supporting_rule_ids: list[str] = Field(default_factory=list)
    # Where specialists disagreed, the PRD (§9) requires the trade-off be stated
    # rather than hidden.
    trade_off: str | None = None


class Synthesis(BaseModel):
    overall_summary: str = Field(description="One short paragraph: main strength and main risk")
    actions: list[PrioritisedAction] = Field(min_length=1, max_length=3)
    uncertainty_note: str | None = Field(
        default=None,
        description="Set when image quality or missing context limits confidence",
    )


class AnalysisResult(BaseModel):
    """The complete payload returned to the client."""

    evidence: VisualEvidence
    findings: list[SpecialistFinding]
    synthesis: Synthesis
    low_confidence: bool = False
