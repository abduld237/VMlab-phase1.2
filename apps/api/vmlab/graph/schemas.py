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
