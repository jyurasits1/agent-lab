#!/usr/bin/env python3
"""
pdf_to_text.py — Convert a PDF to a plain-text file.

Strategy:
  1. Try pdftotext (poppler) to extract embedded text.
  2. If extracted text is too small (<50 non-whitespace chars), treat the PDF
     as a scanned document and OCR it:
       a. Convert pages to PNG images with pdftoppm (poppler).
       b. Run tesseract on each image.
       c. Concatenate results.

Usage:
    python tools/pdf_to_text.py <input.pdf> [output.txt]

If output.txt is omitted the result is written next to the input file with a
.txt extension.
"""

import os
import shutil
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require(cmd: str) -> str:
    """Return the full path of *cmd* or exit with a helpful message."""
    path = shutil.which(cmd)
    if path is None:
        print(
            f"ERROR: '{cmd}' not found on PATH.\n"
            f"  Install it and make sure it is available before running this tool.\n"
            f"  macOS:  brew install {'poppler' if cmd in ('pdftotext', 'pdftoppm') else cmd}\n"
            f"  Debian: apt-get install {'poppler-utils' if cmd in ('pdftotext', 'pdftoppm') else cmd}",
            file=sys.stderr,
        )
        sys.exit(1)
    return path


def _page_count(pdf_path: str) -> int:
    """Return the number of pages using pdfinfo, or 0 on failure."""
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        return 0
    try:
        out = subprocess.check_output(
            [pdfinfo, pdf_path], stderr=subprocess.DEVNULL, text=True
        )
        for line in out.splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":")[1].strip())
    except (subprocess.CalledProcessError, ValueError):
        pass
    return 0


# ---------------------------------------------------------------------------
# Extraction methods
# ---------------------------------------------------------------------------

def extract_with_pdftotext(pdf_path: str) -> str:
    """Run pdftotext and return the extracted text (may be empty)."""
    _require("pdftotext")
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"WARNING: pdftotext exited with code {result.returncode}.\n"
            f"  {result.stderr.strip()}",
            file=sys.stderr,
        )
        return ""
    return result.stdout


def extract_with_ocr(pdf_path: str) -> tuple[str, int]:
    """
    Convert each PDF page to a PNG then OCR it with tesseract.
    Returns (combined_text, pages_processed).
    """
    _require("pdftoppm")
    _require("tesseract")

    with tempfile.TemporaryDirectory(prefix="pdf_to_text_") as tmpdir:
        # Render pages to PNG files named <tmpdir>/page-NNNN.png
        subprocess.run(
            ["pdftoppm", "-png", "-r", "300", pdf_path, os.path.join(tmpdir, "page")],
            check=True,
            capture_output=True,
        )

        pages = sorted(
            f for f in os.listdir(tmpdir) if f.endswith(".png")
        )
        if not pages:
            print("ERROR: pdftoppm produced no images.", file=sys.stderr)
            sys.exit(1)

        texts = []
        for page_file in pages:
            page_path = os.path.join(tmpdir, page_file)
            # tesseract writes <stem>.txt; use stdout via "-" output
            result = subprocess.run(
                ["tesseract", page_path, "stdout", "-l", "eng"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(
                    f"WARNING: tesseract failed on {page_file}: {result.stderr.strip()}",
                    file=sys.stderr,
                )
            texts.append(result.stdout)

        return "\n\n".join(texts), len(pages)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    pdf_path = sys.argv[1]

    if not os.path.isfile(pdf_path):
        print(f"ERROR: File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
    else:
        base, _ = os.path.splitext(pdf_path)
        out_path = base + ".txt"

    # ---- Attempt 1: embedded text ----------------------------------------
    _require("pdftotext")  # fail early with a clear message
    text = extract_with_pdftotext(pdf_path)
    non_ws = len(text.replace(" ", "").replace("\n", "").replace("\t", ""))

    if non_ws >= 50:
        method = "text (pdftotext)"
        pages = _page_count(pdf_path)
    else:
        # ---- Attempt 2: OCR ------------------------------------------------
        print(
            f"Embedded text too small ({non_ws} non-whitespace chars). "
            "Falling back to OCR…",
            file=sys.stderr,
        )
        text, pages = extract_with_ocr(pdf_path)
        method = "OCR (pdftoppm + tesseract)"

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    pages_str = str(pages) if pages else "unknown"
    print(
        f"Done.\n"
        f"  Method : {method}\n"
        f"  Pages  : {pages_str}\n"
        f"  Output : {out_path}"
    )


if __name__ == "__main__":
    main()
