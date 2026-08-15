"""Split the two merged Commercial bundles into their canonical documents.

The client delivered ~30 canonical knowledge-base documents as two merged PDFs
with misleading filenames. Chunking them as-is would stamp the wrong
``document_id`` on 800+ pages and make every rule citation untrustworthy, so
this runs before any ingestion.

Two things the raw bundles hide, both handled here:

* Documents 11 and 12 each appear twice. Document 12 is explicit about it
  (v1.0, then a v2.0 "Detailed and In-Depth Edition"); Document 11 ships two
  editions both labelled v1.0, the second roughly twice the length. The earlier
  edition of each is tagged ``superseded`` so that GRA-089 holds at retrieval
  time -- superseded chunks must never outrank active canonical content.
* The three 14A documents are consumer-psychology standards filed under
  Commercial. They are re-domained to ``retail_psychology`` here, which is most
  of what closes the apparent coverage gap in that specialist.

Writes one PDF per document to data/split/ plus a manifest.json carrying the
metadata that ingestion stamps onto every chunk.
"""

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from pypdf import PdfReader, PdfWriter

SRC = Path("/home/remon/Documents/client/RVMLab_extracted/R.VMLab/Commercial Op.")
OUT = Path(__file__).resolve().parent.parent / "data" / "split"

BUNDLES = {
    "bundle_01_16": "VMlab_Document_01_Cover_Page_Document_Control_Executive_Summary_Domain_Purpose (1).pdf",
    "bundle_17_30": "Document.pdf",
}

HEADING = re.compile(r"^\s*DOCUMENT\s+(\d{1,2}[A-Z]?(?:\.\d)?)\s*$", re.IGNORECASE)

# Documents 24/27/29/30 (and the 25/26 workbooks) describe how the pipeline
# should observe, score and rank. They configure the system rather than feeding
# a specialist, so they get their own domain and stay out of specialist recall.
DOMAINS = {
    "01": "commercial", "02": "commercial", "03": "commercial", "04": "commercial",
    "05": "retail_psychology", "06": "retail_psychology",
    "07": "retail_psychology", "08": "retail_psychology",
    "09": "creative_vm", "10": "creative_vm", "11": "creative_vm", "12": "creative_vm",
    "14A.1": "retail_psychology", "14A.2": "retail_psychology", "14A.3": "retail_psychology",
    "16": "commercial", "17": "commercial", "18": "commercial", "19": "commercial",
    "20": "commercial", "21": "commercial", "22": "commercial", "23": "commercial",
    "24": "system", "27": "system", "29": "system", "30": "system",
}

RULE_PREFIXES = {
    # Document 11 splits its rules across many small families rather than one
    # numbered series. Document 14A.2 is narrative and carries no rule IDs.
    "11": ["NAV", "SIG", "FLW", "ZON", "ENT", "FIX", "ACC", "ADJ", "OPS", "GEN", "PRO", "MEA", "DIG"],
    "12": ["PPS"],
    "14A.1": ["CP"], "14A.2": [], "14A.3": ["APMDF"],
    "16": ["PCE"], "17": ["PAC"], "18": ["IPL"], "19": ["SAG"], "20": ["COS"],
    "21": ["SSM"], "22": ["CPF"], "23": ["KMM", "CDF"], "24": ["AOF"],
    "27": ["PSR"], "29": ["BRK"], "30": ["GRA"],
}


@dataclass
class Document:
    document_id: str
    doc_number: str
    title: str
    domain: str
    rule_prefixes: list[str]
    version: str
    validity_status: str
    authority: str
    source_bundle: str
    source_pages: str
    page_count: int
    path: str


def find_boundaries(reader: PdfReader) -> list[tuple[int, str]]:
    """Return (page_index, doc_number) for every canonical document start.

    Only standalone headings count. Cross-reference tables render as
    "Document 13 - Hero Products & ..." on one line and are skipped, since a
    title follows the number.
    """
    found: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages):
        for line in (page.extract_text() or "").splitlines():
            match = HEADING.match(line)
            if match:
                found.append((index, match.group(1).upper()))
                break
    return found


def read_title(reader: PdfReader, start: int) -> str:
    """Pull the document title from its cover page.

    The title sits between the "DOCUMENT nn" heading and the first control or
    descriptor line, usually in caps across one or two lines.
    """
    lines = [
        line.strip()
        for line in (reader.pages[start].extract_text() or "").splitlines()
        if line.strip()
    ]
    title_parts: list[str] = []
    seen_heading = False
    for line in lines:
        if HEADING.match(line):
            seen_heading = True
            continue
        if not seen_heading:
            continue
        lowered = line.lower()
        if lowered.startswith(("canonical", "customer psychology", "version", "classification", "control")):
            break
        title_parts.append(line)
        if len(title_parts) == 3:
            break
    return " ".join(title_parts).title() or f"Document {start}"


def read_version(reader: PdfReader, start: int, end: int) -> str:
    for index in range(start, min(start + 3, end + 1)):
        text = reader.pages[index].extract_text() or ""
        match = re.search(r"Version\s*\|?\s*(\d+\.\d+)", text) or re.search(
            r"Version\s+(\d+\.\d+)", text
        )
        if match:
            return match.group(1)
    return "1.0"


def split_bundle(label: str, filename: str) -> list[Document]:
    path = SRC / filename
    reader = PdfReader(str(path))
    boundaries = find_boundaries(reader)
    if not boundaries:
        raise SystemExit(f"no document boundaries found in {filename}")

    documents: list[Document] = []
    for position, (start, number) in enumerate(boundaries):
        end = (
            boundaries[position + 1][0] - 1
            if position + 1 < len(boundaries)
            else len(reader.pages) - 1
        )
        documents.append(
            Document(
                document_id=f"VMLAB-KB-{number}",
                doc_number=number,
                title=read_title(reader, start),
                domain=DOMAINS.get(number, "commercial"),
                rule_prefixes=RULE_PREFIXES.get(number, []),
                version=read_version(reader, start, end),
                validity_status="active",
                authority="canonical",
                source_bundle=label,
                source_pages=f"{start + 1}-{end + 1}",
                page_count=end - start + 1,
                path="",
            )
        )

    mark_superseded(documents)

    for document, (start, _) in zip(documents, boundaries):
        end = start + document.page_count - 1
        suffix = "" if document.validity_status == "active" else "_superseded"
        out_path = OUT / f"VMLAB-KB-{document.doc_number}{suffix}.pdf"

        writer = PdfWriter()
        for index in range(start, end + 1):
            writer.add_page(reader.pages[index])
        with out_path.open("wb") as handle:
            writer.write(handle)
        document.path = str(out_path)

    return documents


def mark_superseded(documents: list[Document]) -> None:
    """Where a document number appears more than once, only the last edition stays active.

    Both Document 11 editions claim v1.0, so length is the tiebreaker the
    version string cannot provide -- the in-depth edition is the longer one and
    always comes later in the bundle.
    """
    seen: dict[str, list[Document]] = {}
    for document in documents:
        seen.setdefault(document.doc_number, []).append(document)

    for number, group in seen.items():
        if len(group) == 1:
            continue
        active = group[-1]
        for index, document in enumerate(group[:-1]):
            document.validity_status = "superseded"
            # Ingestion identifies a document by (document_id, version), so two
            # editions both claiming v1.0 collide: writing the second deletes
            # the first. Document 11 lost its 60-page current edition to its own
            # 29-page predecessor that way. Give the older editions a distinct
            # version so both survive and the active one stays retrievable.
            if document.version == active.version:
                document.version = f"{document.version}-prior{index + 1}"
        print(
            f"  ! Document {number} has {len(group)} editions "
            f"({', '.join(f'{d.page_count}pp v{d.version}' for d in group)}) "
            f"-> keeping the last as active"
        )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.pdf"):
        stale.unlink()

    all_documents: list[Document] = []
    for label, filename in BUNDLES.items():
        print(f"\n=== {label}")
        documents = split_bundle(label, filename)
        all_documents.extend(documents)
        for document in documents:
            flag = "" if document.validity_status == "active" else "  [SUPERSEDED]"
            print(
                f"  {document.document_id:<16} {document.domain:<18} "
                f"pp {document.source_pages:<9} v{document.version}{flag}"
            )

    manifest = OUT / "manifest.json"
    manifest.write_text(json.dumps([asdict(d) for d in all_documents], indent=2))

    active = [d for d in all_documents if d.validity_status == "active"]
    print(f"\n{len(all_documents)} documents written ({len(active)} active), manifest at {manifest}")
    print("\nPages per domain (active only):")
    by_domain: dict[str, int] = {}
    for document in active:
        by_domain[document.domain] = by_domain.get(document.domain, 0) + document.page_count
    for domain, pages in sorted(by_domain.items(), key=lambda item: -item[1]):
        print(f"  {domain:<18} {pages:>4} pp")

    missing = {"13", "15", "28"} - {d.doc_number for d in all_documents}
    if missing:
        print(f"\nReferenced but absent from the package: Documents {', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
