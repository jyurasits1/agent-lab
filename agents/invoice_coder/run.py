#!/usr/bin/env python3
"""
invoice_coder/run.py

Reads .txt files from agents/invoice_coder/inbox/, extracts structured invoice
data using heuristic/regex-based parsing (no external dependencies), and writes
the result to agents/invoice_coder/out/invoices.json.

Usage:
    python agents/invoice_coder/run.py
    python agents/invoice_coder/run.py --dry-run
    python agents/invoice_coder/run.py --review --report
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
INBOX_DIR = SCRIPT_DIR / "inbox"
OUT_DIR = SCRIPT_DIR / "out"
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "latest.log"

RAW_EXCERPT_LEN = 500

# ---------------------------------------------------------------------------
# Vendor detection
# ---------------------------------------------------------------------------
KNOWN_VENDORS = [
    (re.compile(r"home\s+depot", re.IGNORECASE), "The Home Depot"),
    (re.compile(r"homedepot\.com", re.IGNORECASE), "The Home Depot"),
    (re.compile(r"lowe['']?s", re.IGNORECASE), "Lowe's"),
    (re.compile(r"menards", re.IGNORECASE), "Menards"),
    (re.compile(r"ace\s+hardware", re.IGNORECASE), "Ace Hardware"),
    (re.compile(r"harbor\s+freight", re.IGNORECASE), "Harbor Freight"),
    (re.compile(r"grainger", re.IGNORECASE), "Grainger"),
    (re.compile(r"fastenal", re.IGNORECASE), "Fastenal"),
    (re.compile(r"amazon", re.IGNORECASE), "Amazon"),
]

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Date: MM/DD/YYYY, YYYY-MM-DD, or Month DD YYYY
DATE_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b"
    r"|\b(\d{4}-\d{2}-\d{2})\b"
    r"|\b([A-Z][a-z]+ \d{1,2},?\s*\d{4})\b"
)

TIME_RE = re.compile(
    r"\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)(?:\s*[A-Z]{2,4})?)\b"
)

# Dollar amount: $1,234.56
MONEY_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")

# SKU: 6+ digit numeric sequence
SKU_RE = re.compile(r"\b(\d{6,})\b")

# Unit price: $X.XX / each|box|roll|...
UNIT_PRICE_RE = re.compile(
    r"\$\s*([\d,]+\.\d{2})\s*/\s*(?:each|box|roll|sheet|pkg|bag|pair|ft|sq\.?\s*ft|linear|lin\.?\s*ft|pc|pcs|unit)",
    re.IGNORECASE,
)

STORE_NUM_RE = re.compile(r"(?:store\s*#|store\s+no\.?)\s*(\w+)", re.IGNORECASE)
STORE_PHONE_RE = re.compile(
    r"store\s+phone\s*#?\s*([\(\d][\d\s\-\.\(\)]{7,})", re.IGNORECASE
)
SALES_PERSON_RE = re.compile(r"sales\s+person\s+(\S+)", re.IGNORECASE)
PO_RE = re.compile(
    r"(?:order\s*#|po\s*#|po\s+number|ofdler\s*#|order\s+no\.?)\s*([A-Z0-9\-]+)",
    re.IGNORECASE,
)
JOB_NAME_RE = re.compile(
    r"(?:po\s*/\s*job\s+name|job\s+name|job\s+#)\s+(.+?)(?:\n|$)", re.IGNORECASE
)
INVOICE_TYPE_RE = re.compile(
    r"\b(receipt|invoice|estimate|quote|purchase\s+order|delivery\s+ticket|work\s+order)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def parse_money(s: str) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def extract_vendor(text: str) -> str | None:
    for pattern, name in KNOWN_VENDORS:
        if pattern.search(text):
            return name
    return None


def extract_invoice_type(text: str) -> str | None:
    m = INVOICE_TYPE_RE.search(text)
    return m.group(1).lower().replace(" ", "_") if m else None


def extract_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    raw = next(g for g in m.groups() if g).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def extract_time(text: str) -> str | None:
    m = TIME_RE.search(text)
    return m.group(1).strip() if m else None


def extract_store_number(text: str) -> str | None:
    m = STORE_NUM_RE.search(text)
    return m.group(1).strip() if m else None


def extract_store_phone(text: str) -> str | None:
    m = STORE_PHONE_RE.search(text)
    return m.group(1).strip() if m else None


def extract_sales_person(text: str) -> str | None:
    m = SALES_PERSON_RE.search(text)
    return m.group(1).strip() if m else None


def extract_po_number(text: str) -> str | None:
    m = PO_RE.search(text)
    return m.group(1).strip() if m else None


def extract_job_name(text: str) -> str | None:
    m = JOB_NAME_RE.search(text)
    return m.group(1).strip() if m else None


def extract_vendor_location(text: str) -> str | None:
    # Prefer an explicit "Location" label
    loc_m = re.search(r"location\s*[§#@:»]\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if loc_m:
        return loc_m.group(1).strip()
    # Fallback: street address line followed by city/state/zip on next line
    addr_m = re.search(
        r"(\d+\s+[A-Z][A-Z\s]+(?:STREET|ST|AVENUE|AVE|ROAD|RD|BLVD|DR|DRIVE|WAY|LANE|LN|COURT|CT)[^\n]*\n[^\n]*[A-Z]{2}\s+\d{5})",
        text,
        re.IGNORECASE,
    )
    if addr_m:
        return re.sub(r"\s+", " ", addr_m.group(1)).strip()
    return None


# ---------------------------------------------------------------------------
# Line item parsing
# ---------------------------------------------------------------------------

def find_item_section(text: str) -> str | None:
    """Return text between the column-header row and the totals block."""
    header_m = re.search(
        r"(?:model\s*#|item\s*#|#\s*item).*?(?:sku\s*#|sku|item).*?(?:unit\s*price|price).*?(?:qty|quantity).*?(?:subtotal|total)",
        text,
        re.IGNORECASE,
    )
    if not header_m:
        return None
    start = header_m.end()
    total_m = re.search(
        r"\b(?:subtotal|sub-total|order\s+total|grand\s+total)\b",
        text[start:],
        re.IGNORECASE,
    )
    end = start + total_m.start() if total_m else len(text)
    return text[start:end]


def parse_line_items(text: str) -> list[dict]:
    section = find_item_section(text)
    if not section:
        return []

    # Split on lines that begin with a 2-digit item number
    raw_chunks = re.split(r"(?m)^(?=\d{2}\s)", section)
    items = []

    for chunk in raw_chunks:
        chunk = chunk.strip()
        if not chunk or not re.match(r"^\d{2}\s", chunk):
            continue

        # Remove "PREFERRED PRICING" discount annotation lines
        chunk_clean = re.sub(r"[®©*]?\s*PREFERRED PRICING[^\n]*\n?", "", chunk)

        # Flatten to one line for regex extraction
        flat = re.sub(r"\s+", " ", chunk_clean).strip()
        # Strip leading item number
        flat = re.sub(r"^\d{2}\s+", "", flat)

        # SKU: first 6+ digit sequence
        sku_m = SKU_RE.search(flat)
        sku = sku_m.group(1) if sku_m else None

        # Unit price: $X.XX / each|box|...
        up_m = UNIT_PRICE_RE.search(flat)
        unit_price = parse_money(up_m.group(1)) if up_m else None

        # All dollar amounts; subtotal is the last one
        all_dollars = [parse_money(v) for v in MONEY_RE.findall(flat)]
        subtotal = all_dollars[-1] if all_dollars else None

        # Qty: last small integer (1–999) after stripping money and SKU strings
        qty = None
        flat_stripped = re.sub(r"\$[\d,]+\.\d{2}", "", flat)
        if sku:
            flat_stripped = flat_stripped.replace(sku, "")
        qty_candidates = re.findall(r"\b([1-9]\d{0,2})\b", flat_stripped)
        for c in reversed(qty_candidates):
            v = int(c)
            if 1 <= v <= 999:
                qty = float(v)
                break

        # Description: text before the SKU (or end of flat if no SKU)
        if sku_m:
            desc = flat[: sku_m.start()]
        else:
            desc = flat
        desc = re.sub(r"\s+N/A\s*$", "", desc, flags=re.IGNORECASE).strip()
        desc = re.sub(r"\s+", " ", desc).strip()

        items.append({
            "description": desc,
            "sku": sku,
            "model": None,
            "unit_price": unit_price,
            "qty": qty,
            "subtotal": subtotal,
        })

    return items


# ---------------------------------------------------------------------------
# Totals extraction
# ---------------------------------------------------------------------------

def extract_totals(text: str) -> dict:
    subtotal = None
    tax = None
    total = None

    sub_m = re.search(r"sub[\s-]?total\s*\$?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    if sub_m:
        subtotal = parse_money(sub_m.group(1))

    tax_m = re.search(r"(?:sales\s+)?tax\s*\$?\s*([\d,]+\.\d{2})", text, re.IGNORECASE)
    if tax_m:
        tax = parse_money(tax_m.group(1))

    for pattern in [
        r"order\s+total\s*\$?\s*([\d,]+\.\d{2})",
        r"grand\s+total\s*\$?\s*([\d,]+\.\d{2})",
        r"(?<!\w)total\s*\$?\s*([\d,]+\.\d{2})",
    ]:
        tot_m = re.search(pattern, text, re.IGNORECASE)
        if tot_m:
            total = parse_money(tot_m.group(1))
            break

    return {"subtotal": subtotal, "tax": tax, "total": total}


# ---------------------------------------------------------------------------
# Core invoice parsing
# ---------------------------------------------------------------------------

def parse_invoice(path: Path) -> tuple[dict, list[str], list[str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    assumptions: list[str] = []
    questions: list[str] = []

    vendor_name = extract_vendor(text)
    if vendor_name is None:
        assumptions.append(f"{path.name}: vendor could not be identified from known patterns.")
        questions.append(f"{path.name}: What vendor issued this invoice?")

    vendor_location = extract_vendor_location(text)
    invoice_type = extract_invoice_type(text)
    invoice_date = extract_date(text)
    invoice_time = extract_time(text)
    job_name = extract_job_name(text)
    po_number = extract_po_number(text)
    sales_person = extract_sales_person(text)
    store_number = extract_store_number(text)
    store_phone = extract_store_phone(text)
    line_items = parse_line_items(text)
    totals = extract_totals(text)

    if not line_items:
        questions.append(f"{path.name}: No line items detected — manual review recommended.")
    if invoice_date is None:
        questions.append(f"{path.name}: Could not extract an invoice date.")
    if totals["total"] is None:
        questions.append(f"{path.name}: Could not extract an order total.")

    invoice = {
        "source_file": path.name,
        "vendor_name": vendor_name,
        "vendor_location": vendor_location,
        "invoice_type": invoice_type,
        "invoice_date": invoice_date,
        "invoice_time": invoice_time,
        "job_name": job_name,
        "po_number": po_number,
        "sales_person": sales_person,
        "store_number": store_number,
        "store_phone": store_phone,
        "line_items": line_items,
        "totals": totals,
        "raw_text_excerpt": text[:RAW_EXCERPT_LEN],
    }
    return invoice, assumptions, questions


# ---------------------------------------------------------------------------
# Process all inbox files
# ---------------------------------------------------------------------------

def process_inbox(inbox_dir: Path) -> tuple[dict, list[dict]]:
    txt_files = sorted(inbox_dir.glob("*.txt"))
    if not txt_files:
        logging.warning("No .txt files found in %s", inbox_dir)
        return {"invoices": [], "assumptions": [], "questions": []}, []

    all_invoices: list[dict] = []
    all_assumptions: list[str] = []
    all_questions: list[str] = []
    file_stats: list[dict] = []

    for txt_file in txt_files:
        logging.info("Processing %s", txt_file.name)
        invoice, assumptions, questions = parse_invoice(txt_file)
        all_invoices.append(invoice)
        all_assumptions.extend(assumptions)
        all_questions.extend(questions)
        file_stats.append({
            "file": txt_file.name,
            "line_items": len(invoice["line_items"]),
            "totals_found": sum(1 for v in invoice["totals"].values() if v is not None),
        })

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped_assumptions = [a for a in all_assumptions if not (a in seen or seen.add(a))]  # type: ignore[func-returns-value]
    seen = set()
    deduped_questions = [q for q in all_questions if not (q in seen or seen.add(q))]  # type: ignore[func-returns-value]

    logging.info(
        "Done: %d invoice(s), %d assumption(s), %d question(s)",
        len(all_invoices), len(deduped_assumptions), len(deduped_questions),
    )
    return (
        {"invoices": all_invoices, "assumptions": deduped_assumptions, "questions": deduped_questions},
        file_stats,
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

REQUIRED_INVOICE_FIELDS = {
    "source_file", "vendor_name", "vendor_location", "invoice_type",
    "invoice_date", "invoice_time", "job_name", "po_number", "sales_person",
    "store_number", "store_phone", "line_items", "totals", "raw_text_excerpt",
}


def generate_report(
    result: dict,
    file_stats: list[dict],
    out_path: Path,
    report_path: Path,
    ran_at: str,
) -> str:
    invoices = result.get("invoices", [])

    plan_lines = [
        "## Plan\n",
        "- **Discover** all `.txt` files in `inbox/`, sorted alphabetically.",
        "- **Parse** each file with regex heuristics: detect vendor from a known-vendor table, "
          "extract labeled fields (Store #, Sales Person, PO/Job Name, etc.), "
          "parse numbered line items (01 … NN format), and extract totals.",
        "- **Best-effort**: set `null` when a field cannot be reliably extracted.",
        "- **Merge** invoices, assumptions, and questions across all files; deduplicate.",
        f"- **Write** `{out_path.name}` and `{report_path.name}` to `out/`.",
        f"- **Append** a run entry to `logs/latest.log`.",
    ]

    exec_lines = ["## Execution\n"]
    for stat in file_stats:
        exec_lines.append(
            f"- `{stat['file']}` — {stat['line_items']} line item(s) extracted, "
            f"{stat['totals_found']}/3 totals fields found"
        )
    exec_lines.append(f"- **Total invoices**: {len(invoices)}")
    exec_lines.append(f"- **Assumptions**: {len(result.get('assumptions', []))}")
    exec_lines.append(f"- **Questions**: {len(result.get('questions', []))}")
    exec_lines.append(f"- Run timestamp: `{ran_at}`")

    checks: list[tuple[bool, str]] = []

    try:
        json.loads(json.dumps(result))
        checks.append((True, "JSON serialises and parses without error"))
    except Exception as exc:
        checks.append((False, f"JSON validity: {exc}"))

    for key in ("invoices", "assumptions", "questions"):
        checks.append((key in result, f"Top-level key `{key}` present"))

    missing_fields = [
        inv.get("source_file", "?")
        for inv in invoices
        if not REQUIRED_INVOICE_FIELDS.issubset(inv.keys())
    ]
    checks.append((
        not missing_fields,
        "All invoices have required schema fields"
        + (f" (missing in: {missing_fields})" if missing_fields else ""),
    ))

    no_items = [inv["source_file"] for inv in invoices if not inv.get("line_items")]
    checks.append((
        not no_items,
        "All invoices have at least one line item"
        + (f" (empty: {no_items})" if no_items else ""),
    ))

    verify_lines = ["## Verification\n"]
    for passed, label in checks:
        verify_lines.append(f"{'- [x]' if passed else '- [ ]'} {label}")
    all_passed = all(p for p, _ in checks)
    verify_lines.append(
        f"\n**Overall**: {'all checks passed' if all_passed else 'one or more checks FAILED'}"
    )

    header = ["# invoice_coder report", "", f"_Generated: {ran_at}_", ""]
    return "\n".join(header + plan_lines + [""] + exec_lines + [""] + verify_lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(dry_run: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if not dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )


def print_review(result: dict) -> None:
    invoices = result.get("invoices", [])
    print("\n── Invoices ────────────────────────────────────────────────")
    for inv in invoices:
        print(f"  {inv['source_file']}")
        print(f"    vendor:  {inv['vendor_name']}")
        print(f"    date:    {inv['invoice_date']}  time: {inv['invoice_time']}")
        print(f"    job:     {inv['job_name']}   PO: {inv['po_number']}")
        print(f"    items:   {len(inv['line_items'])}")
        t = inv["totals"]
        print(f"    totals:  sub={t['subtotal']}  tax={t['tax']}  total={t['total']}")
    print("\n── Assumptions ─────────────────────────────────────────────")
    for a in result.get("assumptions", []):
        print(f"  • {a}")
    print("\n── Questions ───────────────────────────────────────────────")
    for q in result.get("questions", []):
        print(f"  ? {q}")
    print()


def write_outputs(
    result: dict,
    file_stats: list[dict],
    out_path: Path,
    report_path: Path,
    ran_at: str,
    write_report: bool,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logging.info("Wrote %s", out_path)
    print(f"invoices.json written to {out_path}")
    if write_report:
        report_md = generate_report(result, file_stats, out_path, report_path, ran_at)
        report_path.write_text(report_md, encoding="utf-8")
        logging.info("Wrote %s", report_path)
        print(f"report.md  written to {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract structured invoice data from .txt files in inbox/."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print JSON to stdout without writing any files.",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Also write out/report.md with Plan / Execution / Verification sections.",
    )
    parser.add_argument(
        "--review", action="store_true",
        help="Print a concise summary and prompt before writing files.",
    )
    args = parser.parse_args()

    setup_logging(dry_run=args.dry_run)
    logging.info(
        "invoice_coder starting (dry_run=%s, report=%s, review=%s)",
        args.dry_run, args.report, args.review,
    )

    ran_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result, file_stats = process_inbox(INBOX_DIR)

    out_path = OUT_DIR / "invoices.json"
    report_path = OUT_DIR / "report.md"

    if args.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        logging.info("Dry run complete — no files written.")
        return

    if args.review:
        print_review(result)
        try:
            answer = input("Write outputs? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in ("y", "yes"):
            write_outputs(result, file_stats, out_path, report_path, ran_at, args.report)
        else:
            logging.info("Review aborted — no files written.")
            print("Aborted. No files written.")
        return

    write_outputs(result, file_stats, out_path, report_path, ran_at, args.report)


if __name__ == "__main__":
    main()
