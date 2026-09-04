"""The per-analysis priority mix, and what a weight actually does.

The client's design deck puts a slider under each specialist, set just before
the review runs, captioned "when increased, VMlab will...". This module is the
only place that decides what "increased" means, because two callers need the
same answer: the API validates the mix on the way in, and the specialist node
turns a weight into prompt text on the way through.

Weights sum to exactly 100 and floor at 10. The floor is the load-bearing part
-- with three sliders and a fixed total, a zero is always reachable, and a zero
would silence a perspective the PRD requires every analysis to carry (FR-07,
FR-08, FR-09). Ten percent is quiet, not absent.

Item counts stay inside the PRD's three-to-five band. The slider's visible
effect is mostly depth: a muted specialist writes headlines, a dominant one
writes out its reasoning and follows the secondary points. Stretching the count
outside 3-5 would have given the slider more travel and broken the contract to
get it.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field, model_validator

from vmlab.graph.schemas import Perspective

# Each slider's range. The ceiling follows from the floor: with two others held
# at 10, the third cannot exceed 80.
MIN_WEIGHT = 10
MAX_WEIGHT = 80
TOTAL_WEIGHT = 100


class Priorities(BaseModel):
    """How much of the review each specialist owns, as percentages summing to 100.

    Validation lives here rather than in the route so that the API, the graph
    and the tests all agree on what a legal mix is. A request carrying a mix
    that does not add up is a 422 with a readable reason, not a silently
    normalised set of numbers -- quietly rescaling someone's sliders would mean
    the analysis was run against a mix they never chose.
    """

    creative_vm: int = Field(ge=MIN_WEIGHT, le=MAX_WEIGHT)
    retail_psychology: int = Field(ge=MIN_WEIGHT, le=MAX_WEIGHT)
    commercial: int = Field(ge=MIN_WEIGHT, le=MAX_WEIGHT)

    @model_validator(mode="after")
    def must_total_one_hundred(self) -> Priorities:
        total = sum(self.as_dict().values())
        if total != TOTAL_WEIGHT:
            raise ValueError(
                f"the three priorities must add up to {TOTAL_WEIGHT}, not {total}"
            )
        return self

    def as_dict(self) -> dict[str, int]:
        """Keyed by perspective value, which is what the graph state carries."""
        return {perspective.value: getattr(self, perspective.value) for perspective in Perspective}


def default_priorities() -> Priorities:
    """A balanced mix, as close to even as three integers reach.

    34/33/33 rather than 33/33/33: the total is a hard constraint, so the
    remainder has to land somewhere. All three sit in the band that reproduces
    the pipeline's behaviour before sliders existed, which is what makes an
    omitted mix a no-op rather than a silent change of output.
    """
    return Priorities(creative_vm=34, retail_psychology=33, commercial=33)


def balanced_weights() -> dict[str, int]:
    return default_priorities().as_dict()


@dataclass(frozen=True)
class Band:
    """What one weight is worth: how many items, and written how fully."""

    name: str
    min_items: int
    max_items: int
    depth: str


# Four bands over 10-80. The middle band is deliberately the old wording
# verbatim, so a balanced mix produces what the client has already seen.
_BANDS: tuple[tuple[int, Band], ...] = (
    (
        20,
        Band(
            name="muted",
            min_items=3,
            max_items=3,
            depth=(
                "Produce exactly 3 items. This perspective is a low priority for "
                "this review, so report only your headline points -- the findings "
                "you would raise if you had one minute. Keep each reason to a "
                "single sentence."
            ),
        ),
    ),
    (
        45,
        Band(
            name="standard",
            min_items=3,
            max_items=4,
            depth=(
                "Produce between 3 and 4 items. Three is a floor, not a target: a "
                "display always affords at least three observations from your "
                "perspective, even if some are minor or provisional. Lower the "
                "confidence on a weaker item and say what is missing, rather than "
                "omitting it."
            ),
        ),
    ),
    (
        65,
        Band(
            name="emphasised",
            min_items=4,
            max_items=5,
            depth=(
                "Produce between 4 and 5 items. This perspective is a priority for "
                "this review, so go past the obvious: after the headline findings, "
                "cover the secondary points you would otherwise leave out. Write "
                "each reason out in full rather than asserting it."
            ),
        ),
    ),
    (
        MAX_WEIGHT,
        Band(
            name="lead",
            min_items=5,
            max_items=5,
            depth=(
                "Produce exactly 5 items. This perspective is the focus of this "
                "review. Work through the display systematically and report "
                "everything your perspective can support, including the minor and "
                "the provisional -- marked as such. Write each reason out in full, "
                "showing how the evidence and the excerpts lead to it."
            ),
        ),
    ),
)


def band_for(weight: int) -> Band:
    """The band a weight falls in. Out-of-range weights clamp rather than raise.

    `Priorities` already rejects anything outside 10-80, so a stray value here
    came from a stored row or a direct call to `run_analysis`. Clamping keeps
    an odd number from failing an analysis over a presentation decision.
    """
    for ceiling, band in _BANDS:
        if weight <= ceiling:
            return band
    return _BANDS[-1][1]


def lead_perspective(weights: dict[str, int]) -> Perspective | None:
    """The perspective that owns contested ground, or None if nothing leads.

    Ties break on declaration order rather than on whichever key the dict
    happened to yield first, so the same mix always names the same leader. A
    balanced mix has no meaningful leader, so 34/33/33 returns None -- naming
    creative_vm the lead there would hand it every overlapping point on the
    strength of a rounding remainder.
    """
    if not weights:
        return None
    ranked = sorted(Perspective, key=lambda p: (-weights.get(p.value, 0), list(Perspective).index(p)))
    top = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    if runner_up is not None and weights.get(top.value, 0) - weights.get(runner_up.value, 0) < 5:
        return None
    return top
