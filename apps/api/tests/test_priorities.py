"""The priority mix: what a legal mix is, and what a weight buys.

These are the rules the UI sliders and the API both have to agree on, so they
are tested against the model rather than against either caller.
"""

import pytest
from pydantic import ValidationError

from vmlab.graph.priorities import (
    MAX_WEIGHT,
    MIN_WEIGHT,
    Priorities,
    balanced_weights,
    band_for,
    default_priorities,
    lead_perspective,
)
from vmlab.graph.schemas import Perspective


def test_the_default_mix_is_balanced_and_adds_up():
    weights = balanced_weights()
    assert sum(weights.values()) == 100
    assert set(weights) == {p.value for p in Perspective}
    # As close to even as three integers reach; the remainder has to land
    # somewhere and it must not move between releases.
    assert weights == {"creative_vm": 34, "retail_psychology": 33, "commercial": 33}


def test_the_default_mix_reproduces_the_original_wording():
    """A caller that sets no mix must get the pipeline it had before sliders.

    The middle band carries the pre-slider instruction verbatim, so this is the
    guarantee that adding the feature changed nothing for anyone who ignores it.
    """
    for weight in balanced_weights().values():
        band = band_for(weight)
        assert band.name == "standard"
        assert "Three is a floor, not a target" in band.depth


def test_a_mix_that_does_not_total_one_hundred_is_rejected():
    with pytest.raises(ValidationError) as caught:
        Priorities(creative_vm=34, retail_psychology=33, commercial=32)
    # The message names the total it did reach, because "invalid" alone leaves
    # the caller guessing which slider to move.
    assert "not 99" in str(caught.value)


def test_no_perspective_can_be_silenced():
    with pytest.raises(ValidationError):
        Priorities(creative_vm=0, retail_psychology=50, commercial=50)
    with pytest.raises(ValidationError):
        Priorities(creative_vm=MIN_WEIGHT - 1, retail_psychology=50, commercial=41)


def test_the_ceiling_follows_from_the_floor():
    # Two others held at the floor is the most a single slider can take.
    assert Priorities(creative_vm=MAX_WEIGHT, retail_psychology=10, commercial=10)
    with pytest.raises(ValidationError):
        Priorities(creative_vm=MAX_WEIGHT + 1, retail_psychology=10, commercial=9)


@pytest.mark.parametrize(
    ("weight", "name", "min_items", "max_items"),
    [
        (10, "muted", 3, 3),
        (20, "muted", 3, 3),
        (21, "standard", 3, 4),
        (45, "standard", 3, 4),
        (46, "emphasised", 4, 5),
        (65, "emphasised", 4, 5),
        (66, "lead", 5, 5),
        (80, "lead", 5, 5),
    ],
)
def test_band_boundaries(weight, name, min_items, max_items):
    band = band_for(weight)
    assert (band.name, band.min_items, band.max_items) == (name, min_items, max_items)


def test_every_band_stays_inside_the_prd_range():
    """FR-07/08/09 fix each perspective at three to five items.

    The slider moves depth further than it moves count, and this is why: the
    count is bounded by a contract the client signed.
    """
    for weight in range(MIN_WEIGHT, MAX_WEIGHT + 1):
        band = band_for(weight)
        assert 3 <= band.min_items <= band.max_items <= 5


def test_the_anti_padding_rule_survives_every_band():
    """A wide slider must not become a licence for generic filler.

    The rule sits outside the band text precisely so that no band can drop it:
    the depth directive is interpolated *into* the sentence that forbids
    padding, so asking for five items and asking for three carry the same
    prohibition.
    """
    from vmlab.graph import prompts

    for weight in (10, 30, 50, 80):
        rendered = prompts.SPECIALIST_SYSTEM.format(
            brief="", depth=band_for(weight).depth, ownership=""
        )
        assert "Padding with generic retail advice" in rendered


def test_an_out_of_range_weight_clamps_rather_than_raising():
    """A stored row or a direct run_analysis call can carry anything."""
    assert band_for(-5).name == "muted"
    assert band_for(999).name == "lead"


def test_a_balanced_mix_has_no_lead_perspective():
    # 34/33/33 leads on a rounding remainder, which is no reason to hand one
    # perspective every contested point.
    assert lead_perspective(balanced_weights()) is None


def test_the_heaviest_slider_leads():
    weights = {"creative_vm": 15, "retail_psychology": 15, "commercial": 70}
    assert lead_perspective(weights) is Perspective.COMMERCIAL


def test_a_tie_at_the_top_leads_nobody():
    weights = {"creative_vm": 45, "retail_psychology": 45, "commercial": 10}
    assert lead_perspective(weights) is None


def test_the_default_object_and_the_dict_agree():
    assert default_priorities().as_dict() == balanced_weights()
