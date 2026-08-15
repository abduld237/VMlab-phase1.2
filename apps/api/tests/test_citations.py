"""Citation integrity and specialist envelope repair.

Both behaviours here are regressions from a real run against a real photograph.
The commercial specialist cited V20-10.1, V16-19.3, V16-12.1 and V19-18.1 --
plausible-looking identifiers that exist nowhere in the corpus -- and the
creative_vm specialist dropped out entirely because the model narrated instead
of returning the {"items": [...]} envelope.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vmlab.graph.nodes.reasoning import (  # noqa: E402
    _coerce_items,
    _enforce_citations,
    _permitted_rule_ids,
)
from vmlab.graph.schemas import SpecialistItem  # noqa: E402
from vmlab.retrieval.retriever import RetrievedChunk  # noqa: E402


def chunk(rule_ids: list[str]) -> RetrievedChunk:
    return RetrievedChunk(
        id="c1", document_id="VMLAB-KB-12", title="Product Presentation Standards",
        domain="creative_vm", authority="canonical", content="...",
        section_path=None, page=1, rule_ids=rule_ids, distance=0.1,
    )


def item(rule_ids: list[str]) -> SpecialistItem:
    return SpecialistItem(
        observation="no price visible", recommendation="add price tickets",
        reason="the evidence supports it", confidence=0.8, severity="high",
        supporting_rule_ids=rule_ids,
    )


# -- citation integrity -----------------------------------------------------


def test_fabricated_rule_ids_are_dropped():
    permitted = _permitted_rule_ids([chunk(["PPS-032", "PAC-004"])])
    items = [item(["PPS-032", "V20-10.1", "V16-19.3"])]

    _enforce_citations(items, permitted, "commercial")

    assert items[0].supporting_rule_ids == ["PPS-032"], (
        "only identifiers present in retrieved evidence may survive"
    )


def test_a_document_id_is_not_a_rule_id():
    # The retail_psychology specialist cited VMLAB-KB-14A.3, which is real but
    # is a document, not a rule. It must not pass as a rule citation.
    permitted = _permitted_rule_ids([chunk(["APMDF-014"])])
    items = [item(["VMLAB-KB-14A.3"])]

    _enforce_citations(items, permitted, "retail_psychology")

    assert items[0].supporting_rule_ids == []


def test_no_retrieved_rules_means_no_citations_survive():
    items = [item(["PPS-032"])]
    _enforce_citations(items, _permitted_rule_ids([chunk([])]), "creative_vm")
    assert items[0].supporting_rule_ids == []


def test_genuine_citations_are_preserved():
    permitted = _permitted_rule_ids([chunk(["PPS-032"]), chunk(["NAV-007"])])
    items = [item(["PPS-032", "NAV-007"])]

    _enforce_citations(items, permitted, "creative_vm")

    assert items[0].supporting_rule_ids == ["PPS-032", "NAV-007"]


# -- envelope repair --------------------------------------------------------

BODY = {
    "observation": "o", "recommendation": "r", "reason": "because",
    "confidence": 0.5, "severity": "low", "supporting_rule_ids": [],
}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([BODY], id="bare top-level array"),
        pytest.param({"items": [BODY]}, id="already correct"),
        pytest.param({"findings": [BODY]}, id="findings alias"),
        pytest.param({"recommendations": [BODY]}, id="recommendations alias"),
        pytest.param({"analysis": "thinking out loud", "results": [BODY]}, id="narration plus alias"),
        pytest.param({"whatever_key": [BODY]}, id="single unrecognised list"),
    ],
)
def test_near_miss_envelopes_are_repaired(payload):
    assert _coerce_items(payload) == {"items": [BODY]}


def test_unrecoverable_payload_is_left_alone_for_the_retry():
    # Pure narration carries no items; repairing is impossible and the caller
    # must retry rather than silently produce an empty perspective.
    payload = {"analysis": "We need to consider the display holistically."}
    assert _coerce_items(payload) == payload


def test_ambiguous_multiple_lists_are_not_guessed():
    payload = {"good": [BODY], "bad": [BODY]}
    assert _coerce_items(payload) == payload


# -- truncation must not widen the permitted set ----------------------------


def test_permitted_ids_follow_what_the_model_was_shown():
    """A rule truncated out of the prompt must not remain citable.

    _render_knowledge drops chunks that do not fit the character budget. If the
    permitted set were built from everything retrieved, the model would be
    offered ids whose rule text it never saw -- an invitation to cite blind.
    """
    from vmlab.graph.nodes.reasoning import MAX_KNOWLEDGE_CHARS, _render_knowledge

    big = RetrievedChunk(
        id="c1", document_id="VMLAB-KB-12", title="t", domain="creative_vm",
        authority="canonical", content="x" * (MAX_KNOWLEDGE_CHARS - 100),
        section_path=None, page=1, rule_ids=["PPS-001"], distance=0.1,
    )
    truncated_away = RetrievedChunk(
        id="c2", document_id="VMLAB-KB-12", title="t", domain="creative_vm",
        authority="canonical", content="y" * 5000,
        section_path=None, page=2, rule_ids=["PPS-999"], distance=0.2,
    )

    _, shown = _render_knowledge([big, truncated_away])
    permitted = _permitted_rule_ids(shown)

    assert permitted == {"PPS-001"}
    assert "PPS-999" not in permitted, "a rule cut from the prompt cannot be cited"


def test_citable_chunks_win_the_room_within_an_authority_tier():
    """A rule-bearing chunk outranks a rule-less one of the same authority.

    Retrieval routinely returns canonical chunks with no rule ids alongside ones
    that carry them. When only some fit the budget, packing in raw rank order
    left the citable chunks out and the specialist had nothing it was allowed to
    cite -- which is how a real run produced three perspectives and zero
    citations.
    """
    from vmlab.graph.nodes.reasoning import _render_knowledge

    def big(name: str, rule_ids: list[str], authority: str = "canonical") -> RetrievedChunk:
        return RetrievedChunk(
            id=name, document_id=name, title=name, domain="commercial",
            authority=authority, content="z" * 9000, section_path=None, page=1,
            rule_ids=rule_ids, distance=0.1,
        )

    # Ranked first but uncitable; ranked second but carries a rule.
    _, shown = _render_knowledge([big("no-rules", []), big("has-rules", ["PAC-004"])])

    assert [c.document_id for c in shown] == ["has-rules"]
    assert _permitted_rule_ids(shown) == {"PAC-004"}


def test_authority_still_outranks_citability():
    """Doc 29 §13: the governing rule wins, but illustrative never overtakes canonical."""
    from vmlab.graph.nodes.reasoning import _render_knowledge

    def big(name: str, rule_ids: list[str], authority: str) -> RetrievedChunk:
        return RetrievedChunk(
            id=name, document_id=name, title=name, domain="commercial",
            authority=authority, content="z" * 9000, section_path=None, page=1,
            rule_ids=rule_ids, distance=0.1,
        )

    _, shown = _render_knowledge([
        big("illustrative-with-rules", ["PAC-004"], "illustrative"),
        big("canonical-plain", [], "canonical"),
    ])

    assert [c.document_id for c in shown] == ["canonical-plain"]


# -- the retry instruction must describe the schema being asked for ---------


def test_retry_instruction_names_the_right_schema():
    """A shared retry helper must not hardcode one caller's keys.

    Baking the specialist keys into _call_json meant a failed synthesis retry
    instructed the model to return {"items": [...]}. It complied, so every
    remaining attempt failed validation against Synthesis and a completed
    analysis was thrown away at the last step.
    """
    from vmlab.graph.nodes.reasoning import SpecialistItems, _top_level_keys
    from vmlab.graph.schemas import Synthesis

    assert _top_level_keys(SpecialistItems) == '"items"'

    synthesis_keys = _top_level_keys(Synthesis)
    assert '"overall_summary"' in synthesis_keys
    assert '"actions"' in synthesis_keys
    assert '"items"' not in synthesis_keys


# --- the three-item floor ---------------------------------------------------
#
# FR-07/08/09 require three to five items per perspective. On the benchmark set
# one specialist returned a single item, because the prompt used to say "fewer
# is acceptable" and the model took that exit -- the same way an earlier version
# took the exit offered on citations.


def test_the_envelope_rejects_a_thin_response():
    from pydantic import ValidationError

    from vmlab.graph.nodes.reasoning import SpecialistItems

    with pytest.raises(ValidationError):
        SpecialistItems.model_validate({"items": [item([]).model_dump()] * 2})

    # Three is fine.
    assert len(SpecialistItems.model_validate({"items": [item([]).model_dump()] * 3}).items) == 3


def test_a_short_answer_is_salvaged_rather_than_dropped():
    # Two grounded findings beat a missing perspective. The floor drives a
    # retry; this is what happens when the retry does not help either.
    from vmlab.graph.nodes.reasoning import CallOutcome, _salvage_items
    from vmlab.models.openrouter import Completion

    payload = json.dumps({"items": [item(["PPS-001"]).model_dump(), item([]).model_dump()]})
    outcome = CallOutcome(completion=Completion(text=payload, model="m"), attempts=3)

    salvaged = _salvage_items(outcome)

    assert len(salvaged) == 2


def test_salvage_skips_items_that_are_themselves_invalid():
    from vmlab.graph.nodes.reasoning import CallOutcome, _salvage_items
    from vmlab.models.openrouter import Completion

    payload = json.dumps({"items": [item([]).model_dump(), {"observation": "incomplete"}]})
    outcome = CallOutcome(completion=Completion(text=payload, model="m"), attempts=3)

    assert len(_salvage_items(outcome)) == 1


def test_salvage_returns_nothing_when_there_is_nothing_to_salvage():
    from vmlab.graph.nodes.reasoning import CallOutcome, _salvage_items
    from vmlab.models.openrouter import Completion

    assert _salvage_items(None) == []
    assert _salvage_items(CallOutcome(completion=None)) == []
    assert _salvage_items(CallOutcome(completion=Completion(text="not json", model="m"))) == []
