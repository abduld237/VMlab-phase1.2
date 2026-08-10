"""Harvest retail-display artwork from the client's VM Display decks as dev fixtures.

Note what this does NOT produce. The plan assumed these decks would supply the
reference photographs the client promised on 16 July and never sent. They do
not. The three PPTX decks hold campaign artwork, logos and CGI room mockups,
and the brand VM guideline PDFs hold floor plans, vector prop drawings and
rendered planograms. Sampling across all three decks and the two most
photographic PDFs turned up no in-store photography at all.

What the PDFs do contain is genuine rendered planograms -- Clinique counters,
window schemes, fixture layouts -- with hero products, signage, shelving and
product hierarchy. Those are useful for exercising the pipeline during
development, so this collects them as fixtures.

They are NOT a benchmark set. The PRD (11.1) wants strong, average, poor and at
least one low-quality image, and renders are idealised by construction: perfect
lighting, no clutter, no real-world execution faults. The benchmark set still
has to come from real photographs -- see docs/OPEN_DEPENDENCIES.md.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

SRC = Path("/home/remon/Documents/client/RVMLab_extracted/R.VMLab/VM Display ")
OUT = Path(__file__).resolve().parent.parent / "data" / "fixtures"

# Decks that are mostly photographic/rendered rather than technical drawings,
# judged by their JPEG-to-other encoding ratio.
DECKS = [
    "Clinique+Visual+Merchandising+Guideline.pdf",
    "130122_BB4_INSTORE GUIDELINE V2.pdf",
    "btmh_christmas_window_guide_211019.pdf",
    "050122_BB4 Window Presentation.pdf",
    "SOTF - Apartments 3 & 7.pdf",
]

MIN_EDGE = 500
MIN_PIXELS = 400_000
ASPECT_LIMIT = 3.0


def usable(image: Image.Image) -> bool:
    width, height = image.size
    if min(width, height) < MIN_EDGE or width * height < MIN_PIXELS:
        return False
    if max(width, height) / min(width, height) > ASPECT_LIMIT:
        return False
    sample = image.convert("RGB").resize((64, 64))
    # Renders and photographs both carry wide colour range; flat logos do not.
    return len(sample.getcolors(maxcolors=4096) or []) >= 500


def harvest(deck: str, records: list[dict]) -> None:
    path = SRC / deck
    if not path.exists():
        print(f"  {deck[:46]:<46} MISSING")
        return

    kept = 0
    with tempfile.TemporaryDirectory() as tmp:
        # -j keeps JPEG-encoded images in their original encoding, which is
        # where the rendered display artwork lives.
        subprocess.run(
            ["pdfimages", "-j", str(path), f"{tmp}/img"],
            capture_output=True,
            check=False,
        )
        for extracted in sorted(Path(tmp).glob("img-*.jpg")):
            try:
                image = Image.open(extracted)
                image.load()
            except Exception:
                continue
            if not usable(image):
                continue
            name = f"{path.stem[:26].strip().replace(' ', '_')}__{extracted.stem}.jpg"
            target = OUT / name
            shutil.copy(extracted, target)
            records.append(
                {
                    "file": name,
                    "source": deck,
                    "kind": "rendered_planogram",
                    "width": image.size[0],
                    "height": image.size[1],
                }
            )
            kept += 1
    print(f"  {deck[:46]:<46} kept {kept:>3}")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    records: list[dict] = []
    print("Harvesting display artwork from VM guideline PDFs:")
    for deck in DECKS:
        harvest(deck, records)

    (OUT / "fixtures.json").write_text(json.dumps(records, indent=2))
    print(f"\n{len(records)} dev fixtures in {OUT}")
    print("These are rendered planograms, not photographs.")
    print("The PRD benchmark set is still an open dependency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
