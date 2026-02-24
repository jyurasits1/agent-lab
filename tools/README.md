# Tools

Utility scripts for agents and local workflows. All tools are standard-library Python unless noted.

---

## pdf_to_text.py

Convert a PDF to a plain `.txt` file for agent ingestion.

**External dependencies** (must be on `PATH`):

| Tool | Package | Purpose |
|------|---------|---------|
| `pdftotext` | poppler | Extract embedded text |
| `pdftoppm` | poppler | Render pages to PNG (OCR path) |
| `pdfinfo` | poppler | Count pages (optional) |
| `tesseract` | tesseract-ocr | OCR scanned pages |

Install on macOS: `brew install poppler tesseract`
Install on Debian/Ubuntu: `apt-get install poppler-utils tesseract-ocr`

**Usage:**

```bash
# Output written to <input>.txt by default
python tools/pdf_to_text.py path/to/document.pdf

# Specify output path explicitly
python tools/pdf_to_text.py path/to/document.pdf path/to/output.txt
```

**How it works:**

1. Runs `pdftotext` to pull embedded text.
2. If the result has fewer than 50 non-whitespace characters (scanned/image PDF), it falls back to OCR:
   - Renders each page at 300 dpi with `pdftoppm`.
   - Runs `tesseract` on each image.
   - Concatenates results into one file.
3. Prints a summary: method used, pages processed, output path.
