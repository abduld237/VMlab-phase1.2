"""Turn extracted document text into retrievable chunks.

The chunking rule is the client's own, from Document 11 §E3: segment at the
lowest complete unit that preserves meaning, keep each chunk's heading path, and
never split a requirement away from its rationale, evidence or exception.

That last clause is the reason this is not a sliding window over characters. The
canonical documents state a rule and then immediately qualify it -- an exception,
a precedence note, a guardrail. A window that cuts between the two produces a
chunk that reads as an unconditional instruction, and Document 30's own ingestion
rule GRA-083 is explicit that enabling knowledge must travel with its guardrails.
So rule blocks are held together even when that pushes a chunk over target size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ~0.75 words per token is close enough for sizing; nothing downstream depends
# on an exact count, and it avoids a tokeniser dependency per model.
WORDS_PER_TOKEN = 0.75

TARGET_TOKENS = 600
MAX_TOKENS = 1000
MIN_TOKENS = 40

# "PCE-001", "APMDF-112", "NAV-004" -- the citation handles that make a
# specialist's output traceable back to the client's own standard.
RULE_ID = re.compile(r"\b([A-Z]{2,10})-(\d{3})\b")

# "1. Purpose", "12.3 Evidence Requirements", "Section 4 -- Scope"
HEADING = re.compile(
    r"^\s*(?:section\s+)?(\d{1,2}(?:\.\d{1,2}){0,2})[.)]?\s+([A-Z][^\n]{3,90})\s*$",
    re.IGNORECASE,
)

# Page furniture that adds nothing and dilutes the embedding.
NOISE = re.compile(
    r"^\s*(?:"
    r"page\s+\d+(\s+of\s+\d+)?"
    r"|VMlab AI Knowledge Base"
    r"|Controlled Document.*"
    r"|Internal Canonical Knowledge.*"
    r"|Classification:.*"
    r"|\d+\s*"
    r")\s*$",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    return int(len(text.split()) / WORDS_PER_TOKEN)


@dataclass
class Chunk:
    content: str
    chunk_index: int
    section_path: str
    page: int | None
    rule_ids: list[str] = field(default_factory=list)
    content_type: str = "text"

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


@dataclass
class Page:
    number: int
    text: str


def clean_lines(text: str) -> list[str]:
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or NOISE.match(stripped):
            continue
        kept.append(stripped)
    return kept


def _block_rule_ids(block: list[str]) -> list[str]:
    found = {f"{m.group(1)}-{m.group(2)}" for line in block for m in RULE_ID.finditer(line)}
    return sorted(found)


def _starts_rule(line: str) -> bool:
    """True when a line opens a canonical rule block.

    Rules in these documents begin with their own ID at the start of the line,
    which is what lets a rule and its trailing qualifiers be kept together.
    """
    match = RULE_ID.match(line)
    return match is not None and match.start() == 0


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    """Chunk a document, tracking the heading path and holding rule blocks intact."""
    chunks: list[Chunk] = []
    section_path = ""
    buffer: list[str] = []
    buffer_page: int | None = None
    # A rule block runs from a line that opens with a rule ID until the next
    # rule or heading, so its qualifiers stay attached.
    in_rule = False

    def flush() -> None:
        nonlocal buffer, buffer_page, in_rule
        if not buffer:
            return
        content = "\n".join(buffer).strip()
        if content and estimate_tokens(content) >= MIN_TOKENS:
            chunks.append(
                Chunk(
                    content=content,
                    chunk_index=len(chunks),
                    section_path=section_path,
                    page=buffer_page,
                    rule_ids=_block_rule_ids(buffer),
                )
            )
        buffer = []
        buffer_page = None
        in_rule = False

    for page in pages:
        for line in clean_lines(page.text):
            heading = HEADING.match(line)
            if heading:
                # A heading always closes the previous unit -- it is the
                # strongest "lowest complete unit" boundary available.
                flush()
                section_path = f"{heading.group(1)} {heading.group(2).strip()}"
                continue

            if _starts_rule(line):
                # Only break between rules, never inside one.
                if in_rule and estimate_tokens("\n".join(buffer)) >= TARGET_TOKENS:
                    flush()
                in_rule = True

            if buffer_page is None:
                buffer_page = page.number
            buffer.append(line)

            # Outside a rule block, split once the chunk reaches target size.
            # Inside one, hold on until MAX so a rule keeps its qualifiers.
            size = estimate_tokens("\n".join(buffer))
            if (not in_rule and size >= TARGET_TOKENS) or size >= MAX_TOKENS:
                flush()

    flush()
    return chunks
