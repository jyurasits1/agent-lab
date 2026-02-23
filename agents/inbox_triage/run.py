#!/usr/bin/env python3
"""
inbox_triage/run.py

Reads .txt files from agents/inbox_triage/inbox/, parses them into structured
tasks using rule-based heuristics (no external dependencies), and writes the
result to agents/inbox_triage/out/tasks.json.

Usage:
    python agents/inbox_triage/run.py
    python agents/inbox_triage/run.py --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (all relative to the repo root, which we derive from this file's
# location: agents/inbox_triage/run.py  → repo_root = ../../)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
INBOX_DIR = SCRIPT_DIR / "inbox"
OUT_DIR = SCRIPT_DIR / "out"
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "latest.log"

# ---------------------------------------------------------------------------
# Heuristic keyword tables
# ---------------------------------------------------------------------------

PRIORITY_SIGNALS = {
    1: [
        "asap", "urgent", "critical", "immediately", "blocker", "blocking",
        "customers are complaining", "production", "outage", "down", "broken",
        "high priority", "priority 1", "p1",
    ],
    2: [
        "soon", "this week", "end of sprint", "needs a fix", "fix asap",
        "important", "deadline", "by end of", "by march", "by april",
        "priority 2", "p2",
    ],
    3: [
        "low priority", "when there's bandwidth", "when bandwidth",
        "cosmetic", "nice to have", "eventually", "no hard deadline",
        "priority 3", "p3",
    ],
}

# Patterns that hint at a due date in prose
DATE_PATTERNS = [
    # "by March 31", "by end of March 31"
    (r"\bby\s+(?:end\s+of\s+)?([A-Z][a-z]+\s+\d{1,2}(?:,\s*\d{4})?)\b", "%B %d"),
    (r"\bby\s+(?:end\s+of\s+)?([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\b", "%B %d, %Y"),
    # "end of Q1", "end of Q2" etc.
    (r"\bend\s+of\s+Q([1-4])\b", "quarter"),
    # ISO dates
    (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
    # "this week" → relative
    (r"\bthis\s+week\b", "this_week"),
]

TAG_KEYWORD_MAP = {
    "bug": ["bug", "fail", "fails", "failure", "broken", "error", "crash", "silent", "promo code"],
    "feature": ["feature", "csv export", "export", "dashboard", "new"],
    "docs": ["runbook", "documentation", "doc", "write up", "writeup", "guide"],
    "tech-debt": ["deprecated", "warning", "warnings", "cosmetic", "legacy", "old"],
    "infra": ["sync job", "data-sync", "nightly", "on-call", "restart", "pipeline", "deploy"],
    "frontend": ["mobile", "checkout", "admin panel", "ui", "ux", "page load"],
    "backend": ["api", "endpoint", "database", "query", "migration", "service"],
    "customer-facing": ["customers", "support tickets", "users", "client"],
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def split_into_items(text: str) -> list[str]:
    """
    Split a block of text into individual task-like items.
    Strategy: numbered lists first, then paragraph breaks.
    """
    # Numbered list: lines starting with "1.", "2.", etc.
    numbered = re.split(r"\n(?=\s*\d+\.\s)", text)
    if len(numbered) > 1:
        # Drop preamble segments that appear before the first numbered item
        return [
            item.strip()
            for item in numbered
            if item.strip() and re.match(r"\s*\d+\.", item.strip())
        ]

    # Fall back: split on double newlines (paragraphs)
    paragraphs = re.split(r"\n{2,}", text)
    if len(paragraphs) > 1:
        return [p.strip() for p in paragraphs if p.strip()]

    # Single block — treat as one item
    return [text.strip()]


def infer_priority(text: str) -> int:
    lower = text.lower()
    for level in (1, 2, 3):
        for kw in PRIORITY_SIGNALS[level]:
            if kw in lower:
                return level
    return 2  # default: medium


def extract_due_date(text: str) -> str | None:
    lower = text.lower()

    # Quarter shorthand
    q_match = re.search(r"end\s+of\s+q([1-4])", lower)
    if q_match:
        q = int(q_match.group(1))
        year = datetime.now(timezone.utc).year
        quarter_end = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[q]
        return f"{year}-{quarter_end}"

    # "this week" → next Sunday
    if "this week" in lower:
        today = datetime.now(timezone.utc)
        days_until_sunday = (6 - today.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        from datetime import timedelta
        due = today + timedelta(days=days_until_sunday)
        return due.strftime("%Y-%m-%d")

    # ISO date
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso:
        return iso.group(1)

    # "by <Month> <day>" — try current year first
    month_day = re.search(
        r"\bby\s+(?:end\s+of\s+)?([A-Z][a-z]+\s+\d{1,2})(?:,\s*(\d{4}))?\b", text
    )
    if month_day:
        date_str = month_day.group(1)
        year_str = month_day.group(2)
        try:
            if year_str:
                dt = datetime.strptime(f"{date_str}, {year_str}", "%B %d, %Y")
            else:
                year = datetime.now(timezone.utc).year
                dt = datetime.strptime(f"{date_str}, {year}", "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def extract_tags(text: str) -> list[str]:
    lower = text.lower()
    tags = []
    for tag, keywords in TAG_KEYWORD_MAP.items():
        if any(kw in lower for kw in keywords):
            tags.append(tag)
    return sorted(tags)


def make_title(item_text: str) -> str:
    """Take the first sentence or first ~80 chars as the title."""
    # Strip leading numbering like "1. " or "- "
    clean = re.sub(r"^\s*[\d]+\.\s*|^\s*[-*]\s*", "", item_text).strip()
    # First sentence
    first_sentence = re.split(r"(?<=[.!?])\s+", clean)[0]
    if len(first_sentence) > 90:
        first_sentence = first_sentence[:87] + "..."
    return first_sentence


def make_summary(item_text: str) -> str:
    """Return a short cleaned-up version of the item text."""
    clean = re.sub(r"^\s*[\d]+\.\s*|^\s*[-*]\s*", "", item_text).strip()
    # Collapse whitespace / newlines
    clean = re.sub(r"\s+", " ", clean)
    if len(clean) > 300:
        clean = clean[:297] + "..."
    return clean


def infer_next_action(text: str, priority: int) -> str:
    lower = text.lower()
    if "bug" in lower or "fail" in lower or "broken" in lower or "complain" in lower:
        return "Reproduce the issue and open a bug ticket with steps to reproduce."
    if "runbook" in lower or "documentation" in lower or "doc" in lower or "write up" in lower:
        return "Draft the document and share with the relevant team for review."
    if "export" in lower or "dashboard" in lower or "feature" in lower:
        return "Create a ticket, estimate effort, and assign to the next sprint."
    if "deprecated" in lower or "warning" in lower or "cosmetic" in lower:
        return "Log a tech-debt ticket; address in the next available maintenance window."
    if priority == 1:
        return "Escalate immediately and assign an owner today."
    if priority == 2:
        return "Add to the current sprint backlog and assign an owner."
    return "Log for future consideration and revisit during next planning cycle."


# ---------------------------------------------------------------------------
# Core triage logic
# ---------------------------------------------------------------------------

def triage_file(path: Path) -> tuple[list[dict], list[str], list[str]]:
    """
    Parse one .txt file and return (tasks, assumptions, questions).
    """
    text = path.read_text(encoding="utf-8")
    items = split_into_items(text)

    tasks = []
    assumptions = []
    questions = []

    # Strip header lines (From:, Date:) before processing items
    header_pattern = re.compile(r"^(From|Date|To|Subject|CC):.*$", re.MULTILINE | re.IGNORECASE)

    for raw_item in items:
        item = header_pattern.sub("", raw_item).strip()
        if not item or len(item) < 20:
            continue

        priority = infer_priority(item)
        due_date = extract_due_date(item)
        tags = extract_tags(item)
        title = make_title(item)
        summary = make_summary(item)
        next_action = infer_next_action(item, priority)

        tasks.append(
            {
                "title": title,
                "summary": summary,
                "priority": priority,
                "due_date": due_date,
                "next_action": next_action,
                "tags": tags,
            }
        )

    # Assumptions we always make about this heuristic approach
    assumptions.append(
        "Priority is inferred from keyword signals; no human review has been applied."
    )
    assumptions.append(
        "Due dates are extracted from date-like phrases in the text; implicit deadlines may be missed."
    )
    assumptions.append(
        f"All items were read from '{path.name}'; the sender's intent may differ from the parsed summary."
    )

    # Surface questions if certain signals are ambiguous
    if any(t["due_date"] is None and t["priority"] <= 2 for t in tasks):
        questions.append(
            "Some medium-or-high-priority items have no detectable due date — can the team confirm deadlines?"
        )
    if any("bug" in t["tags"] for t in tasks):
        questions.append(
            "Bug items were detected — are there existing tickets, or should new ones be created?"
        )
    if len(tasks) > 5:
        questions.append(
            "More than 5 tasks were found in this inbox item — should any be deferred to a later sprint?"
        )

    return tasks, assumptions, questions


def triage_inbox(inbox_dir: Path) -> dict:
    txt_files = sorted(inbox_dir.glob("*.txt"))
    if not txt_files:
        logging.warning("No .txt files found in %s", inbox_dir)
        return {"tasks": [], "assumptions": [], "questions": []}

    all_tasks: list[dict] = []
    all_assumptions: list[str] = []
    all_questions: list[str] = []

    for txt_file in txt_files:
        logging.info("Processing %s", txt_file.name)
        tasks, assumptions, questions = triage_file(txt_file)
        all_tasks.extend(tasks)
        all_assumptions.extend(assumptions)
        all_questions.extend(questions)

    # Deduplicate assumptions / questions while preserving order
    seen: set[str] = set()
    deduped_assumptions = []
    for a in all_assumptions:
        if a not in seen:
            deduped_assumptions.append(a)
            seen.add(a)

    seen = set()
    deduped_questions = []
    for q in all_questions:
        if q not in seen:
            deduped_questions.append(q)
            seen.add(q)

    logging.info(
        "Triage complete: %d task(s), %d assumption(s), %d question(s)",
        len(all_tasks),
        len(deduped_assumptions),
        len(deduped_questions),
    )

    return {
        "tasks": all_tasks,
        "assumptions": deduped_assumptions,
        "questions": deduped_questions,
    }


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triage .txt inbox files into a structured tasks.json."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the output JSON to stdout without writing any files.",
    )
    args = parser.parse_args()

    setup_logging(dry_run=args.dry_run)
    logging.info("inbox_triage starting (dry_run=%s)", args.dry_run)

    result = triage_inbox(INBOX_DIR)
    output_json = json.dumps(result, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(output_json)
        logging.info("Dry run complete — no files written.")
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tasks_path = OUT_DIR / "tasks.json"
        tasks_path.write_text(output_json, encoding="utf-8")
        logging.info("Wrote %s", tasks_path)
        print(f"Output written to {tasks_path}")


if __name__ == "__main__":
    main()
