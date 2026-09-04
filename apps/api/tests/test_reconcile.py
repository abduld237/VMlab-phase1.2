"""The cross-perspective duplicate guard.

The client's report was that the three sections say the same thing. They did,
and for a structural reason: all three specialists reason over one shared
`VisualEvidence`, and the domains genuinely overlap on the same physical facts.
`prompts.SPECIALIST_BRIEFS` asks each of them to stay off the others' ground;
this stage is what makes that true, because three specialists running in
parallel cannot see what they are about to duplicate.

The vectors below are built rather than hand-written. The rule compares each
pair against the spread of that analysis's *own* pairs, on mean-centred
embeddings, so a few tidy axis-aligned vectors produce a degenerate distribution
that tests nothing. Seeded gaussians reproduce the shape of real embeddings and
stay deterministic.
"""

import asyncio
import math
import random
from unittest.mock import patch

import pytest

from vmlab.config import get_settings

from vmlab.graph.nodes.reconcile import reconcile_findings
from vmlab.graph.schemas import Perspective, SpecialistFinding, SpecialistItem

DIM = 32


def unit(seed: int) -> list[float]:
    """A deterministic direction, far from every other seed's."""
    rng = random.Random(seed)
    values = [rng.gauss(0, 1) for _ in range(DIM)]
    length = math.sqrt(sum(value * value for value in values))
    return [value / length for value in values]


def near(vector: list[float], seed: int) -> list[float]:
    """Almost the same direction: two ways of saying one finding."""
    rng = random.Random(seed)
    values = [value + rng.gauss(0, 0.06) for value in vector]
    length = math.sqrt(sum(value * value for value in values))
    return [value / length for value in values]


class VectorStub:
    """Returns a pre-set vector per text, so similarity is decided by the test."""

    def __init__(self, vectors: dict[str, list[float]], fail: bool = False):
        self.vectors = vectors
        self.fail = fail
        self.embed_calls = 0

    async def embed(self, texts, **kwargs):
        self.embed_calls += 1
        if self.fail:
            raise RuntimeError("embeddings endpoint is down")
        return [self.vectors[text] for text in texts]


def item(observation: str) -> SpecialistItem:
    return SpecialistItem(
        observation=observation,
        recommendation="do the thing",
        reason="because the evidence supports it",
        confidence=0.7,
    )


def finding(perspective: Perspective, *observations: str) -> SpecialistFinding:
    return SpecialistFinding(
        perspective=perspective, items=[item(text) for text in observations]
    )


def key(observation: str) -> str:
    """The text reconcile embeds, so a stub can be keyed by it."""
    return f"{observation} do the thing"


def observations(findings, perspective) -> list[str]:
    section = next(f for f in findings if f.perspective is perspective)
    return [entry.observation for entry in section.items]


HEAVY_ON_CREATIVE = {"creative_vm": 60, "retail_psychology": 10, "commercial": 30}


def two_sections(duplicates: int = 1, size: int = 4):
    """Creative and commercial sections whose first `duplicates` items pair up."""
    creative = [f"cvm {n}" for n in range(size)]
    commercial = [f"com {n}" for n in range(size)]
    vectors = {key(text): unit(index) for index, text in enumerate(creative)}
    for index, text in enumerate(commercial):
        vectors[key(text)] = (
            near(vectors[key(creative[index])], 900 + index)
            if index < duplicates
            else unit(100 + index)
        )
    findings = [
        finding(Perspective.CREATIVE_VM, *creative),
        finding(Perspective.COMMERCIAL, *commercial),
    ]
    return findings, VectorStub(vectors)


async def test_a_duplicate_is_dropped_from_the_lower_weighted_perspective():
    findings, client = two_sections()

    result = await reconcile_findings(
        {"findings": findings, "priorities": HEAVY_ON_CREATIVE}, client
    )

    # The heavier slider keeps the contested point; that is what makes the
    # sliders decide ownership rather than only length.
    assert "cvm 0" in observations(result["reconciled"], Perspective.CREATIVE_VM)
    assert "com 0" not in observations(result["reconciled"], Perspective.COMMERCIAL)


async def test_the_lighter_slider_loses_the_point_whichever_way_round_it_is():
    findings, client = two_sections()

    result = await reconcile_findings(
        {
            "findings": findings,
            "priorities": {"creative_vm": 20, "retail_psychology": 10, "commercial": 70},
        },
        client,
    )

    assert "com 0" in observations(result["reconciled"], Perspective.COMMERCIAL)
    assert "cvm 0" not in observations(result["reconciled"], Perspective.CREATIVE_VM)


async def test_the_survivor_credits_the_perspective_that_lost_the_point():
    """The repetition goes, the corroboration signal stays.

    Three specialists independently reaching one finding is a severity signal.
    Deleting the duplicate without recording it would throw that away with the
    noise, and the reader would never know two perspectives had agreed.
    """
    findings, client = two_sections()

    result = await reconcile_findings(
        {"findings": findings, "priorities": HEAVY_ON_CREATIVE}, client
    )

    survivor = next(
        entry
        for entry in next(
            f for f in result["reconciled"] if f.perspective is Perspective.CREATIVE_VM
        ).items
        if entry.observation == "cvm 0"
    )
    assert survivor.also_raised_by == [Perspective.COMMERCIAL]


async def test_distinct_findings_are_left_alone():
    """The rule is relative, so it must not manufacture a duplicate.

    Two standard deviations above the mean always exists. Without the absolute
    floor this analysis -- where no two findings resemble each other -- would
    still have its most similar pair struck out.
    """
    findings, client = two_sections(duplicates=0)

    result = await reconcile_findings(
        {"findings": findings, "priorities": HEAVY_ON_CREATIVE}, client
    )

    assert [len(f.items) for f in result["reconciled"]] == [4, 4]
    assert not any(
        entry.also_raised_by for f in result["reconciled"] for entry in f.items
    )


async def test_a_specialist_repeating_itself_is_left_alone():
    """That is a different fault, and not this stage's to fix.

    Removing it would silently shorten a section the user asked to be long.
    """
    creative = ["cvm 0", "cvm 1", "cvm 2", "cvm 3"]
    commercial = ["com 0", "com 1", "com 2", "com 3"]
    vectors = {key(text): unit(index) for index, text in enumerate(creative)}
    # cvm 1 restates cvm 0 -- inside one perspective.
    vectors[key("cvm 1")] = near(vectors[key("cvm 0")], 901)
    vectors.update({key(text): unit(100 + index) for index, text in enumerate(commercial)})

    result = await reconcile_findings(
        {
            "findings": [
                finding(Perspective.CREATIVE_VM, *creative),
                finding(Perspective.COMMERCIAL, *commercial),
            ],
            "priorities": HEAVY_ON_CREATIVE,
        },
        VectorStub(vectors),
    )

    assert len(observations(result["reconciled"], Perspective.CREATIVE_VM)) == 4


async def test_a_perspective_is_never_reduced_below_two_items():
    """A section showing one line reads as a broken agent, not a quiet one."""
    findings, client = two_sections(duplicates=4, size=4)

    result = await reconcile_findings(
        {"findings": findings, "priorities": HEAVY_ON_CREATIVE}, client
    )

    kept = observations(result["reconciled"], Perspective.COMMERCIAL)
    assert len(kept) == 2, "the floor holds even when everything duplicates"
    # The ones that could not be dropped are still credited, so the reader is
    # not left thinking two perspectives found them independently.
    creative = next(
        f for f in result["reconciled"] if f.perspective is Perspective.CREATIVE_VM
    )
    assert sum(1 for entry in creative.items if entry.also_raised_by) == 4


async def test_a_slow_embedding_call_is_abandoned_rather_than_waited_out():
    """A rate-limited endpoint once backed off for 21s: a third of the budget.

    Deduplication is the one stage in the graph that can be skipped without
    changing what the analysis concludes, so it is the one that should give up
    first when the clock matters.
    """

    class Slow:
        async def embed(self, texts, **kwargs):
            await asyncio.sleep(5)
            raise AssertionError("should have been abandoned before this")

    findings, _ = two_sections()
    with patch.object(get_settings(), "reconcile_embed_timeout_seconds", 0.01):
        result = await reconcile_findings({"findings": findings}, Slow())

    assert result["reconciled"] == findings
    assert result["errors"] == ["duplicate findings were not reconciled: timed out"]


async def test_a_failed_embedding_call_costs_the_dedup_and_nothing_else():
    findings, _ = two_sections()
    client = VectorStub({}, fail=True)

    result = await reconcile_findings({"findings": findings}, client)

    assert result["reconciled"] == findings
    assert result["errors"] and "not reconciled" in result["errors"][0]


async def test_zero_vectors_are_similar_to_nothing():
    """A degraded embedding endpoint must not collapse the whole report.

    Zero-norm vectors are what a stub and a broken endpoint both return, and the
    naive cosine divides by zero on them. Answering "not similar" means a bad
    embedding costs the deduplication and leaves the findings intact.
    """
    findings, client = two_sections()
    client.vectors = {text: [0.0] * DIM for text in client.vectors}

    result = await reconcile_findings({"findings": findings}, client)

    assert [len(f.items) for f in result["reconciled"]] == [4, 4]


async def test_too_few_findings_to_compare_are_passed_through():
    """Mean-centring needs enough vectors for the mean to describe anything."""
    findings, client = two_sections(size=2)

    result = await reconcile_findings(
        {"findings": findings, "priorities": HEAVY_ON_CREATIVE}, client
    )

    assert [len(f.items) for f in result["reconciled"]] == [2, 2]


async def test_a_single_perspective_needs_no_embedding_call():
    findings = [finding(Perspective.CREATIVE_VM, "cvm 0", "cvm 1", "cvm 2")]
    client = VectorStub({})

    result = await reconcile_findings({"findings": findings}, client)

    assert result["reconciled"] == findings
    assert client.embed_calls == 0


async def test_the_original_findings_are_not_mutated():
    """State written by another node must survive a replay unedited."""
    findings, client = two_sections()

    await reconcile_findings(
        {"findings": findings, "priorities": HEAVY_ON_CREATIVE}, client
    )

    assert [len(f.items) for f in findings] == [4, 4]
    assert all(not entry.also_raised_by for f in findings for entry in f.items)


@pytest.mark.parametrize("state", [{}, {"findings": []}])
async def test_no_findings_is_not_an_error(state):
    result = await reconcile_findings(state, VectorStub({}))
    assert result["reconciled"] == []
