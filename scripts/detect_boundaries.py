"""Probe the two merged Commercial bundles and report where each canonical
document starts. Read-only: prints a boundary map for review before splitting.

Boundary headings are standalone centred lines like "DOCUMENT 17" or
"Document 03". Cross-reference tables use "Document 13 - Hero Products & ..."
on the same line, so any line carrying a title after the number is ignored.
"""

import re
import sys
from pathlib import Path

from pypdf import PdfReader

BUNDLES = {
    "bundle_01_16": "VMlab_Document_01_Cover_Page_Document_Control_Executive_Summary_Domain_Purpose (1).pdf",
    "bundle_17_30": "Document.pdf",
}

SRC = Path(
    "/home/remon/Documents/client/RVMLab_extracted/R.VMLab/Commercial Op."
)

# "DOCUMENT 14A.1" / "Document 03" alone on the line, nothing but whitespace after.
HEADING = re.compile(r"^\s*DOCUMENT\s+(\d{1,2}[A-Z]?(?:\.\d)?)\s*$", re.IGNORECASE)


def boundaries(pdf_path: Path) -> list[tuple[int, str]]:
    reader = PdfReader(str(pdf_path))
    found: list[tuple[int, str]] = []
    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for line in text.splitlines():
            match = HEADING.match(line)
            if match:
                found.append((page_no, match.group(1).upper()))
                break  # one boundary per page is enough
    return found


def main() -> int:
    for label, filename in BUNDLES.items():
        path = SRC / filename
        reader = PdfReader(str(path))
        print(f"\n=== {label}: {filename}  ({len(reader.pages)} pages)")
        found = boundaries(path)
        if not found:
            print("  no boundaries detected")
            continue
        for i, (page_no, doc_id) in enumerate(found):
            end = found[i + 1][0] - 1 if i + 1 < len(found) else len(reader.pages)
            print(f"  Document {doc_id:>6}  pages {page_no:>4}-{end:<4} ({end - page_no + 1} pp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
