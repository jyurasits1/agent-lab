# invoice_coder

Reads plain-text invoice/receipt files from `inbox/`, extracts structured data
using heuristic regex-based parsing (no external dependencies), and writes the
result to `out/invoices.json`.

No LLM calls. Python 3.10+ standard library only.

---

## Directory layout

```
agents/invoice_coder/
├── run.py          # main script
├── inbox/          # drop .txt files here (OCR output or plain-text invoices)
│   └── testinvoice.txt
├── out/            # created on first run
│   ├── invoices.json
│   └── report.md   # written with --report
└── README.md
```

Logs are appended to `logs/latest.log` at the repo root (created automatically).

---

## Usage

Run from the **repo root** or from any directory:

```bash
# Normal run — writes out/invoices.json
python agents/invoice_coder/run.py

# Dry run — prints JSON to stdout, writes nothing
python agents/invoice_coder/run.py --dry-run

# Show a human-readable summary and confirm before writing
python agents/invoice_coder/run.py --review

# Write both invoices.json and out/report.md
python agents/invoice_coder/run.py --report

# Full review + report
python agents/invoice_coder/run.py --review --report
```

---

## Output schema

```json
{
  "invoices": [
    {
      "source_file":     "string — filename from inbox/",
      "vendor_name":     "string|null — matched from known-vendor table",
      "vendor_location": "string|null — store address from 'Location' label or address block",
      "invoice_type":    "string|null — receipt | invoice | estimate | quote | …",
      "invoice_date":    "string|null — YYYY-MM-DD normalized when possible",
      "invoice_time":    "string|null — as written (e.g. '9:31 AM EST')",
      "job_name":        "string|null — from 'PO / Job Name' or 'Job Name' label",
      "po_number":       "string|null — from 'Order #' or 'PO #' label",
      "sales_person":    "string|null — from 'Sales Person' label",
      "store_number":    "string|null — from 'Store #' label",
      "store_phone":     "string|null — from 'Store Phone #' label",
      "line_items": [
        {
          "description": "string",
          "sku":         "string|null — first 6+ digit numeric sequence per row",
          "model":       "string|null — null when invoice shows N/A",
          "unit_price":  "number|null — post-discount price preferred",
          "qty":         "number|null",
          "subtotal":    "number|null — last dollar amount on the item row"
        }
      ],
      "totals": {
        "subtotal": "number|null",
        "tax":      "number|null",
        "total":    "number|null"
      },
      "raw_text_excerpt": "string — first 500 chars of source file"
    }
  ],
  "assumptions": ["string"],
  "questions":   ["string"]
}
```

---

## Extraction heuristics

| Field | Strategy |
|-------|----------|
| `vendor_name` | Regex match against a table of known vendor names and URLs |
| `vendor_location` | `Location §/:/# …` label, then fallback street-address block |
| `invoice_type` | First match of receipt / invoice / estimate / quote / … |
| `invoice_date` | First date-like string: MM/DD/YYYY → YYYY-MM-DD, ISO, or Month DD YYYY |
| `invoice_time` | First `H:MM AM/PM [TZ]` pattern |
| `job_name` | `PO / Job Name …` or `Job Name …` label |
| `po_number` | `Order #`, `PO #`, or OCR variant `Ofdler #` label |
| `sales_person` | `Sales Person …` label |
| `store_number` | `Store #` label |
| `store_phone` | `Store Phone # …` label |
| `line_items` | Section between column-header row and totals block; items delimited by leading `NN ` numbers |
| `totals` | `Subtotal`, `Sales Tax`, `Order Total` / `Grand Total` labels |

Fields not matched are set to `null`. OCR noise is tolerated via flexible
patterns (e.g. `Ofdler #` matches `Order #`).

---

## Adding more invoice files

Drop any `.txt` file (OCR output, copy-paste, or hand-typed) into `inbox/` and
re-run. All files are processed and merged into a single `invoices.json`.
