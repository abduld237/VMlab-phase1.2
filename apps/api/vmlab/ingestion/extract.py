"""Pull text out of the corpus formats: PDF, XLSX and PPTX.

PDFs come in two kinds here and they need different handling. The canonical
knowledge-base documents are text-rich and extract cleanly. The brand VM
guideline decks are visual -- 139 to 541 characters per page against 900+ for the
canonical set -- so text extraction returns almost nothing usable and those pages
need a vision caption instead. classify_pdf() reports which kind a file is so the
caller can route it, rather than silently ingesting near-empty pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from vmlab.ingestion.chunking import Page

# Below this, a page is carrying pictures rather than prose. Derived from a
# survey of all 27 corpus PDFs: canonical documents sit at 900-3400 chars/page,
# the visual decks at 139-541.
TEXT_DENSITY_FLOOR = 600


@dataclass
class PdfProfile:
    path: Path
    page_count: int
    chars_per_page: float

    @property
    def is_text_rich(self) -> bool:
        return self.chars_per_page >= TEXT_DENSITY_FLOOR

    @property
    def route(self) -> str:
        return "text" if self.is_text_rich else "vision"


def classify_pdf(path: Path, sample_pages: int = 16) -> PdfProfile:
    """Decide whether a PDF is text-rich or needs the vision route.

    Sampling is spread across the whole document, not taken from the front. The
    brand decks open with text-heavy title, contents and introduction pages and
    then run to full-bleed imagery, so a front-loaded sample rates them
    text-rich and they get ingested as a handful of near-empty chunks -- which
    is exactly what happened before this sampled evenly.
    """
    reader = PdfReader(str(path))
    total = len(reader.pages)
    if total == 0:
        return PdfProfile(path=path, page_count=0, chars_per_page=0.0)

    step = max(1, total // sample_pages)
    indices = list(range(0, total, step))[:sample_pages]

    characters = 0
    for index in indices:
        characters += len((reader.pages[index].extract_text() or "").strip())

    return PdfProfile(
        path=path,
        page_count=total,
        chars_per_page=characters / len(indices),
    )


def read_pdf_pages(path: Path) -> list[Page]:
    reader = PdfReader(str(path))
    return [
        Page(number=index, text=page.extract_text() or "")
        for index, page in enumerate(reader.pages, start=1)
    ]


def read_xlsx_pages(path: Path) -> list[Page]:
    """Render each worksheet as one page of tab-separated rows.

    The structured workbooks (Documents 25 and 26) carry canonical rules and
    schema definitions as spreadsheet rows. Row order is meaningful -- a rule and
    its fields sit on one row -- so rows are kept whole rather than flattened
    cell by cell.
    """
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    pages: list[Page] = []
    for index, sheet in enumerate(workbook.worksheets, start=1):
        lines = [f"# {sheet.title}"]
        for row in sheet.iter_rows(values_only=True):
            cells = [str(value).strip() for value in row if value is not None]
            if cells:
                lines.append("\t".join(cells))
        if len(lines) > 1:
            pages.append(Page(number=index, text="\n".join(lines)))
    workbook.close()
    return pages


def read_pptx_pages(path: Path) -> list[Page]:
    """One page per slide, from its text frames.

    These decks are mostly imagery, so many slides yield little or nothing;
    empty slides are dropped rather than becoming near-empty chunks.
    """
    from pptx import Presentation

    presentation = Presentation(str(path))
    pages: list[Page] = []
    for index, slide in enumerate(presentation.slides, start=1):
        fragments: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    fragments.append(text)
        if fragments:
            pages.append(Page(number=index, text="\n".join(fragments)))
    return pages


def read_document(path: Path) -> list[Page]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf_pages(path)
    if suffix == ".xlsx":
        return read_xlsx_pages(path)
    if suffix == ".pptx":
        return read_pptx_pages(path)
    raise ValueError(f"unsupported document type: {path.suffix}")
