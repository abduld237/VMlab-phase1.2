"""Ingest the knowledge base: extract, chunk, embed, store.

--dry-run does everything except embedding and writing, so chunking can be
validated against the real corpus without an OpenRouter key or a database. Run
it after any change to chunking.py and compare the report -- a sudden swing in
chunk count or a jump in oversized chunks means the rule-block handling has
broken, and that is far cheaper to catch here than in retrieval results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from vmlab.ingestion.chunking import Chunk, chunk_pages  # noqa: E402
from vmlab.ingestion.extract import classify_pdf, read_document  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SPLIT = ROOT / "data" / "split"
CORPUS = Path("/home/remon/Documents/client/RVMLab_extracted/R.VMLab")

# Third-party academic material sitting in the client's Commercial folder. It is
# not his canonical standard, so it must never be tagged canonical -- ingested as
# 'guidance' so authority-first retrieval ranks it below the real thing.
THIRD_PARTY = {
    "K-MeansClustering-BasedMarketBasketAnalysis-U.K.OnlineE-CommerceRetailerIEEEPublished.pdf",
    "content.pdf",
    "vol-2_issue-3.pdf",
    "Principles-Retailing-Course-Taster.pdf",
    "sensors-15-21114.pdf",
}

FOLDER_DOMAIN = {
    "Commercial Op.": "commercial",
    "Retail psychology": "retail_psychology",
    "VM Display ": "creative_vm",
}


@dataclass
class Source:
    path: Path
    document_id: str
    title: str
    domain: str
    authority: str
    validity_status: str
    version: str
    rule_prefixes: list[str]


def canonical_sources() -> list[Source]:
    """The split canonical documents, from the manifest split_bundles.py wrote."""
    manifest_path = SPLIT / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("run scripts/split_bundles.py first -- no manifest found")

    sources = []
    for entry in json.loads(manifest_path.read_text()):
        sources.append(
            Source(
                path=Path(entry["path"]),
                document_id=entry["document_id"],
                title=entry["title"],
                domain=entry["domain"],
                authority="canonical",
                validity_status=entry["validity_status"],
                version=entry["version"],
                rule_prefixes=entry["rule_prefixes"],
            )
        )
    return sources


def supplementary_sources() -> list[Source]:
    """Everything else in the corpus: workbooks, third-party PDFs, brand decks."""
    sources: list[Source] = []
    for folder, domain in FOLDER_DOMAIN.items():
        directory = CORPUS / folder
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if path.name.startswith("~$") or path.suffix.lower() not in {
                ".pdf",
                ".xlsx",
                ".pptx",
            }:
                continue
            # The two merged bundles are already covered by the split manifest.
            if path.name in {
                "Document.pdf",
                "VMlab_Document_01_Cover_Page_Document_Control_Executive_Summary_Domain_Purpose (1).pdf",
            }:
                continue

            is_third_party = path.name in THIRD_PARTY
            is_vmlab_workbook = path.name.startswith("VMlab_Document_")
            sources.append(
                Source(
                    path=path,
                    document_id=path.stem[:60],
                    title=path.stem,
                    domain=domain,
                    authority="guidance" if is_third_party else (
                        "canonical" if is_vmlab_workbook else "illustrative"
                    ),
                    validity_status="active",
                    version="1.0",
                    rule_prefixes=[],
                )
            )
    return sources


def process(source: Source) -> tuple[list[Chunk], str]:
    """Extract and chunk one source. Returns its chunks and the route taken."""
    route = "text"
    if source.path.suffix.lower() == ".pdf":
        profile = classify_pdf(source.path)
        route = profile.route

    pages = read_document(source.path)
    if not pages:
        return [], route
    return chunk_pages(pages), route


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="extract and chunk only; no embedding, no database writes",
    )
    parser.add_argument("--limit", type=int, help="process at most N sources")
    args = parser.parse_args()

    sources = canonical_sources() + supplementary_sources()
    if args.limit:
        sources = sources[: args.limit]

    print(f"{len(sources)} sources to process\n")

    all_chunks: list[tuple[Source, list[Chunk]]] = []
    vision_backlog: list[Source] = []

    for source in sources:
        try:
            chunks, route = process(source)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  ERROR  {source.path.name[:52]:<52} {type(exc).__name__}: {exc}")
            continue

        if route == "vision":
            vision_backlog.append(source)
            marker = "vision"
        else:
            marker = "text  "
        all_chunks.append((source, chunks))
        rules = sum(len(c.rule_ids) for c in chunks)
        print(
            f"  {marker} {source.document_id[:44]:<44} {source.domain:<18} "
            f"{len(chunks):>5} chunks, {rules:>4} rule refs"
        )

    if args.dry_run:
        report(all_chunks, vision_backlog)
        return 0

    return asyncio.run(_write_all(all_chunks))


async def _write_all(all_chunks: list[tuple[Source, list[Chunk]]]) -> int:
    """Embed and store everything. Each document commits on its own.

    Per-document transactions mean an interrupted run leaves whole documents
    written rather than a partial corpus, and re-running only redoes what is
    missing -- which matters when the full pass is ~70 embedding requests over
    several minutes.
    """
    from vmlab.ingestion.writer import DocumentRecord, embed_chunks, write_document
    from vmlab.models.openrouter import OpenRouterClient
    from vmlab.tenancy.session import close_pool, service_session

    total_chunks = sum(len(c) for _, c in all_chunks)
    print(f"\nembedding and writing {total_chunks:,} chunks...")

    written = failed = 0
    async with OpenRouterClient() as client:
        for source, chunks in all_chunks:
            if not chunks:
                continue
            try:
                vectors = await embed_chunks(client, chunks)
                async with service_session() as connection:
                    await write_document(
                        connection,
                        DocumentRecord(
                            document_id=source.document_id,
                            title=source.title,
                            domain=source.domain,
                            authority=source.authority,
                            validity_status=source.validity_status,
                            version=source.version,
                            rule_prefixes=source.rule_prefixes,
                            source_path=str(source.path),
                        ),
                        chunks,
                        vectors,
                    )
                written += 1
                print(f"  ok   {source.document_id[:46]:<46} {len(chunks):>5} chunks")
            except Exception as exc:  # noqa: BLE001 - report and continue
                failed += 1
                print(f"  FAIL {source.document_id[:46]:<46} {type(exc).__name__}: {str(exc)[:90]}")

    await close_pool()
    print(f"\n{written} documents written, {failed} failed")
    return 1 if failed else 0


def report(
    all_chunks: list[tuple[Source, list[Chunk]]], vision_backlog: list[Source]
) -> None:
    chunks = [c for _, group in all_chunks for c in group]
    if not chunks:
        print("\nno chunks produced")
        return

    sizes = sorted(c.tokens for c in chunks)
    by_domain: Counter[str] = Counter()
    for source, group in all_chunks:
        by_domain[source.domain] += len(group)

    oversized = [c for c in chunks if c.tokens > 1000]
    unsectioned = [c for c in chunks if not c.section_path]
    with_rules = [c for c in chunks if c.rule_ids]
    distinct_rules = {rule for c in chunks for rule in c.rule_ids}

    print(f"\n{'=' * 62}")
    print(f"{len(chunks):,} chunks from {len(all_chunks)} sources")
    print(f"\nchunks per domain:")
    for domain, count in by_domain.most_common():
        print(f"  {domain:<20} {count:>6,}")

    print(f"\nchunk size (estimated tokens):")
    print(f"  median {sizes[len(sizes) // 2]:>5}")
    print(f"  p90    {sizes[int(len(sizes) * 0.9)]:>5}")
    print(f"  max    {sizes[-1]:>5}")
    print(f"  oversized (>1000): {len(oversized):,} ({len(oversized) / len(chunks):.1%})")

    print(f"\ntraceability:")
    print(f"  chunks carrying rule IDs   {len(with_rules):>6,} ({len(with_rules) / len(chunks):.1%})")
    print(f"  distinct rules referenced  {len(distinct_rules):>6,}")
    print(f"  chunks with no section path{len(unsectioned):>6,} ({len(unsectioned) / len(chunks):.1%})")

    total_tokens = sum(sizes)
    print(f"\nembedding estimate:")
    print(f"  {total_tokens:,} tokens -> ${total_tokens / 1e6 * 0.01:.3f} at $0.01/M (bge-m3)")

    if vision_backlog:
        pages = 0
        for source in vision_backlog:
            try:
                pages += classify_pdf(source.path).page_count
            except Exception:  # noqa: BLE001
                pass
        print(f"\nvision route: {len(vision_backlog)} image-heavy documents, ~{pages:,} pages")
        print("  these need page rendering + vision captioning, not text extraction")


if __name__ == "__main__":
    raise SystemExit(main())
