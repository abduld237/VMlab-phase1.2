"""Stage 3.5: remove the same finding when two perspectives both made it.

The client's complaint was that the three sections repeat each other. They do,
and for a structural reason: all three specialists reason over one shared
`VisualEvidence`, and the domains genuinely overlap on the same physical facts.
A weak focal point is a composition fault, an attention fault and a
hero-visibility fault at once, so all three report it honestly and the reader
sees one problem three times.

`prompts.SPECIALIST_BRIEFS` asks each specialist to stay off the others' ground.
That is necessary and not sufficient: the three run in parallel and cannot see
each other, so none of them knows what it is about to duplicate. Only something
downstream of the fan-in can. This is that thing -- the same division of labour
as the citation guard, where the prompt names the legal rule ids and
`_enforce_citations` is what makes it true.

The priority mix decides who wins. When two perspectives claim one point, it
stays with the one the user weighted higher and is struck from the other, which
is what makes the sliders mean something beyond length: they decide ownership of
contested ground. The survivor records who else raised it, so the corroboration
signal -- three specialists independently flagging one thing is a severity
signal, not noise -- is kept rather than thrown away with the duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time

from vmlab.config import get_settings
from vmlab.graph.priorities import balanced_weights
from vmlab.graph.schemas import Perspective, SpecialistFinding, SpecialistItem
from vmlab.graph.state import AnalysisState
from vmlab.models.openrouter import OpenRouterClient

logger = logging.getLogger(__name__)

# No perspective is reduced below this, however much it duplicates. A section
# showing one line reads to the client as a broken agent rather than as a quiet
# one, and the whole point of the 10% slider floor is that every perspective
# still says something.
MIN_ITEMS_AFTER_RECONCILE = 2

# Mean-centring needs enough vectors for the mean to describe something. Below
# this there is barely anything to deduplicate anyway, so the stage passes
# through rather than guessing from a two-point distribution. Three perspectives
# at the muted band's three items each is nine, so this only ever fires when
# specialists have dropped out.
MIN_ITEMS_TO_COMPARE = 6


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity, with a zero vector treated as similar to nothing.

    The zero case is not hypothetical: a stub or a degraded embedding endpoint
    returns zeros, and the naive formula divides by zero there. Answering "not
    similar" means a bad embedding costs us the deduplication and nothing else.
    """
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _centre(vectors: list[list[float]]) -> list[list[float]]:
    """Subtract the mean vector, so similarity measures what *differs*.

    Every finding in one analysis describes the same photograph, so they share a
    large common component -- the display, the fixtures, the vocabulary of the
    brief -- and raw cosine mostly measures that. Two findings about completely
    different things still score 0.6. Removing the mean leaves the part that
    distinguishes them, which roughly triples the gap between a real duplicate
    and a merely adjacent finding.
    """
    count = len(vectors)
    width = len(vectors[0])
    mean = [sum(vector[i] for vector in vectors) / count for i in range(width)]
    return [[value - mean[i] for i, value in enumerate(vector)] for vector in vectors]


# Scales a median absolute deviation to the standard deviation it would imply
# for normally distributed data, so `duplicate_sigma` reads as a normal sigma.
_MAD_TO_SIGMA = 1.4826


def _duplicate_threshold(scores: list[float]) -> float:
    """Where this analysis's own distribution stops being ordinary.

    Relative rather than absolute because the absolute level moves between
    analyses; robust rather than mean-and-stdev because both of those are moved
    by the very outliers being looked for. See the note on `duplicate_sigma` in
    config.py for the measurements behind that. The floor is what stops the rule
    inventing duplicates in a report that has none.
    """
    settings = get_settings()
    median = statistics.median(scores)
    deviation = statistics.median([abs(score - median) for score in scores])
    relative = median + settings.duplicate_sigma * _MAD_TO_SIGMA * deviation
    return max(relative, settings.duplicate_similarity_floor)


def _rank(weights: dict[str, int]) -> dict[Perspective, tuple[int, int]]:
    """Sort key per perspective: heaviest first, declaration order breaking ties.

    Returned as a comparable tuple rather than a bare weight so that two
    perspectives on the same weight still resolve deterministically -- otherwise
    which one keeps a contested finding would depend on dict iteration order and
    the same analysis could reconcile differently on a rerun.
    """
    order = list(Perspective)
    return {p: (-weights.get(p.value, 0), order.index(p)) for p in order}


async def reconcile_findings(state: AnalysisState, client: OpenRouterClient) -> dict:
    """Collapse cross-perspective duplicates, crediting the survivor.

    Returns `reconciled` -- a separate key from `findings`, which the three
    specialists write to concurrently through an `operator.add` reducer and so
    cannot be rewritten in place.
    """
    started = time.monotonic()
    findings: list[SpecialistFinding] = list(state.get("findings") or [])

    def elapsed() -> dict:
        return {"stage_timings_ms": {"reconcile": int((time.monotonic() - started) * 1000)}}

    if len(findings) < 2:
        # Nothing can duplicate across perspectives when there is only one.
        return {"reconciled": findings, **elapsed()}

    # (finding index, item index) in the order their texts are embedded.
    coordinates = [
        (f_index, i_index)
        for f_index, finding in enumerate(findings)
        for i_index in range(len(finding.items))
    ]
    texts = [
        f"{findings[f].items[i].observation} {findings[f].items[i].recommendation}"
        for f, i in coordinates
    ]
    if not texts:
        return {"reconciled": findings, **elapsed()}

    try:
        vectors = await asyncio.wait_for(
            client.embed(texts), timeout=get_settings().reconcile_embed_timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 - deduplication is not load-bearing
        # One embedding call is the only thing this stage needs, and losing it
        # costs a tidier report. Losing the analysis would cost the whole run,
        # so it degrades and says so rather than propagating. The timeout is
        # part of that: a rate-limited embeddings endpoint once backed off for
        # 21 seconds, which is a third of the latency target spent on tidying.
        reason = "timed out" if isinstance(exc, TimeoutError) else str(exc)
        logger.warning("could not embed findings for reconciliation: %s", reason)
        return {
            "reconciled": findings,
            "errors": [f"duplicate findings were not reconciled: {reason}"],
            **elapsed(),
        }

    if len(vectors) != len(texts):
        logger.warning(
            "embedding returned %d vectors for %d findings; skipping reconciliation",
            len(vectors), len(texts),
        )
        return {"reconciled": findings, **elapsed()}

    if len(vectors) < MIN_ITEMS_TO_COMPARE:
        return {"reconciled": findings, **elapsed()}

    vectors = _centre(vectors)

    # Every cross-perspective pair, scored once. The threshold is derived from
    # this same set, so it has to be built before anything can be compared
    # against it.
    scored: dict[tuple[int, int], float] = {}
    for left in range(len(coordinates)):
        for right in range(left + 1, len(coordinates)):
            if coordinates[left][0] == coordinates[right][0]:
                continue
            scored[(left, right)] = _cosine(vectors[left], vectors[right])

    if not scored:
        return {"reconciled": findings, **elapsed()}

    threshold = _duplicate_threshold(list(scored.values()))
    logger.info(
        "reconciling %d findings across %d perspectives; duplicate threshold %.3f",
        len(coordinates), len(findings), threshold,
    )
    rank = _rank(state.get("priorities") or balanced_weights())

    # Work on copies: state written by another node must not be mutated in
    # place, or a retry or a checkpoint replays against already-edited data.
    kept: dict[int, list[SpecialistItem]] = {
        f_index: [item.model_copy(deep=True) for item in finding.items]
        for f_index, finding in enumerate(findings)
    }
    dropped: set[tuple[int, int]] = set()

    # Strongest pairs first: when one finding duplicates two others, the closest
    # match should decide its fate rather than whichever came first in index
    # order. Sorted descending, so the first pair below the threshold ends it.
    for (left, right), score in sorted(scored.items(), key=lambda pair: -pair[1]):
        if score < threshold:
            break
        if left in dropped or right in dropped:
            continue

        left_perspective = findings[coordinates[left][0]].perspective
        right_perspective = findings[coordinates[right][0]].perspective
        winner, loser = (
            (left, right)
            if rank[left_perspective] <= rank[right_perspective]
            else (right, left)
        )
        winner_f, winner_i = coordinates[winner]
        loser_f, loser_i = coordinates[loser]
        loser_perspective = findings[loser_f].perspective

        surviving = len(kept[loser_f]) - sum(
            1 for dropped_index in dropped if coordinates[dropped_index][0] == loser_f
        )
        if surviving <= MIN_ITEMS_AFTER_RECONCILE:
            # Credit without dropping. The reader still learns the perspectives
            # agree, and the thin section stays readable.
            logger.info(
                "keeping duplicate in %s (%.3f): dropping it would leave fewer than %d items",
                loser_perspective.value, score, MIN_ITEMS_AFTER_RECONCILE,
            )
            _credit(kept[winner_f][winner_i], loser_perspective)
            continue

        dropped.add(loser)
        _credit(kept[winner_f][winner_i], loser_perspective)
        logger.info(
            "dropped duplicate from %s (%.3f, kept in %s): %s",
            loser_perspective.value, score,
            findings[winner_f].perspective.value,
            findings[loser_f].items[loser_i].recommendation[:120],
        )

    removed = {coordinates[d] for d in dropped}
    reconciled = [
        SpecialistFinding(
            perspective=finding.perspective,
            items=[
                item
                for i_index, item in enumerate(kept[f_index])
                if (f_index, i_index) not in removed
            ],
        )
        for f_index, finding in enumerate(findings)
    ]

    if dropped:
        logger.info("reconciliation removed %d duplicate finding(s)", len(dropped))

    return {"reconciled": reconciled, **elapsed()}


def _credit(item: SpecialistItem, perspective: Perspective) -> None:
    """Record that another perspective independently made this point."""
    if perspective not in item.also_raised_by:
        item.also_raised_by.append(perspective)
