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


def triage_inbox(inbox_dir: Path) -> tuple[dict, list[dict]]:
    """
    Returns (result_dict, file_stats).
    file_stats is a list of {"file": str, "items_extracted": int, "tasks_produced": int}.
    """
    txt_files = sorted(inbox_dir.glob("*.txt"))
    if not txt_files:
        logging.warning("No .txt files found in %s", inbox_dir)
        return {"tasks": [], "assumptions": [], "questions": []}, []

    all_tasks: list[dict] = []
    all_assumptions: list[str] = []
    all_questions: list[str] = []
    file_stats: list[dict] = []

    for txt_file in txt_files:
        logging.info("Processing %s", txt_file.name)
        tasks, assumptions, questions = triage_file(txt_file)
        # items_extracted = raw split count before filtering; approximate via summary length
        raw_items = split_into_items(txt_file.read_text(encoding="utf-8"))
        file_stats.append(
            {
                "file": txt_file.name,
                "items_extracted": len(raw_items),
                "tasks_produced": len(tasks),
            }
        )
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

    return (
        {
            "tasks": all_tasks,
            "assumptions": deduped_assumptions,
            "questions": deduped_questions,
        },
        file_stats,
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

REQUIRED_TASK_FIELDS = {"title", "summary", "priority", "due_date", "next_action", "tags"}


def generate_report(
    result: dict,
    file_stats: list[dict],
    tasks_path: Path,
    report_path: Path,
    ran_at: str,
) -> str:
    tasks = result.get("tasks", [])

    # ---- Plan section -------------------------------------------------------
    plan_lines = [
        "## Plan\n",
        "- **Discover** all `.txt` files in `inbox/`, sorted alphabetically.",
        "- **Parse** each file: strip email-style headers (`From:`, `Date:`, etc.), "
          "then split into items using numbered-list markers, falling back to paragraph breaks.",
        "- **Apply heuristics** per item: infer priority from keyword signals, "
          "extract due dates from prose patterns (ISO, month-day, quarter, relative), "
          "assign tags from keyword groups, generate a title and summary.",
        "- **Filter** items that start before the first numbered entry (preamble) "
          "and items shorter than 20 characters after header stripping.",
        "- **Merge** tasks, assumptions, and questions across all files; "
          "deduplicate assumptions and questions.",
        f"- **Write** `{tasks_path.name}` to `out/` and append a run entry to `logs/latest.log`.",
        f"- **Write** `{report_path.name}` to `out/` (this file).",
    ]

    # ---- Execution section --------------------------------------------------
    exec_lines = ["## Execution\n"]
    for stat in file_stats:
        exec_lines.append(
            f"- `{stat['file']}` — {stat['items_extracted']} raw item(s) extracted, "
            f"{stat['tasks_produced']} task(s) produced after filtering"
        )
    exec_lines.append(f"- **Total tasks**: {len(tasks)}")
    exec_lines.append(f"- **Assumptions**: {len(result.get('assumptions', []))}")
    exec_lines.append(f"- **Questions**: {len(result.get('questions', []))}")
    exec_lines.append(f"- **tasks.json** written to `{tasks_path}`")
    exec_lines.append(f"- **report.md** written to `{report_path}`")
    exec_lines.append(f"- Run timestamp: `{ran_at}`")

    # ---- Verification section -----------------------------------------------
    checks: list[tuple[bool, str]] = []

    # 1. JSON valid — if we got here, it parsed fine; double-check by re-serialising
    try:
        json.loads(json.dumps(result))
        checks.append((True, "JSON serialises and parses without error"))
    except Exception as exc:
        checks.append((False, f"JSON validity: {exc}"))

    # 2. Top-level schema keys
    for key in ("tasks", "assumptions", "questions"):
        checks.append((key in result, f"Top-level key `{key}` present"))

    # 3. Each task has required fields
    tasks_missing_fields = [
        t.get("title", "<no title>")
        for t in tasks
        if not REQUIRED_TASK_FIELDS.issubset(t.keys())
    ]
    checks.append((
        not tasks_missing_fields,
        "All tasks have required fields"
        + (f" (missing in: {tasks_missing_fields})" if tasks_missing_fields else ""),
    ))

    # 4. Priority in 1–5
    bad_priority = [t.get("title", "?") for t in tasks if t.get("priority") not in range(1, 6)]
    checks.append((
        not bad_priority,
        "All task priorities are in range 1–5"
        + (f" (bad: {bad_priority})" if bad_priority else ""),
    ))

    # 5. No empty titles
    empty_titles = [i for i, t in enumerate(tasks) if not t.get("title")]
    checks.append((
        not empty_titles,
        "No tasks have an empty title"
        + (f" (indices: {empty_titles})" if empty_titles else ""),
    ))

    verify_lines = ["## Verification\n"]
    for passed, label in checks:
        mark = "- [x]" if passed else "- [ ]"
        verify_lines.append(f"{mark} {label}")

    all_passed = all(p for p, _ in checks)
    verify_lines.append(
        f"\n**Overall**: {'all checks passed' if all_passed else 'one or more checks FAILED'}"
    )

    # ---- Assemble -----------------------------------------------------------
    header = [
        f"# inbox_triage report",
        f"",
        f"_Generated: {ran_at}_",
        f"",
    ]
    sections = (
        header
        + plan_lines
        + [""]
        + exec_lines
        + [""]
        + verify_lines
    )
    return "\n".join(sections) + "\n"


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
    """Print a concise human-readable summary of the triage result."""
    tasks = result.get("tasks", [])
    print("\n── Tasks ──────────────────────────────────────────────────")
    for i, t in enumerate(tasks, 1):
        due = t["due_date"] or "no due date"
        print(f"  {i}. [P{t['priority']}] {t['title']}")
        print(f"       due: {due}")
        print(f"       next: {t['next_action']}")
    print("\n── Assumptions ─────────────────────────────────────────────")
    for a in result.get("assumptions", []):
        print(f"  • {a}")
    print("\n── Questions ───────────────────────────────────────────────")
    for q in result.get("questions", []):
        print(f"  ? {q}")
    print()


def write_outputs(result: dict, file_stats: list[dict], tasks_path: Path,
                  report_path: Path, ran_at: str, write_report: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    tasks_path.write_text(output_json, encoding="utf-8")
    logging.info("Wrote %s", tasks_path)
    print(f"tasks.json written to {tasks_path}")

    if write_report:
        report_md = generate_report(result, file_stats, tasks_path, report_path, ran_at)
        report_path.write_text(report_md, encoding="utf-8")
        logging.info("Wrote %s", report_path)
        print(f"report.md  written to {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triage .txt inbox files into a structured tasks.json."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the output JSON to stdout without writing any files.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Also write out/report.md with Plan / Execution / Verification sections.",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Print a concise task summary and prompt before writing any files.",
    )
    args = parser.parse_args()

    setup_logging(dry_run=args.dry_run)
    logging.info(
        "inbox_triage starting (dry_run=%s, report=%s, review=%s)",
        args.dry_run, args.report, args.review,
    )

    ran_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result, file_stats = triage_inbox(INBOX_DIR)

    tasks_path = OUT_DIR / "tasks.json"
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
            write_outputs(result, file_stats, tasks_path, report_path, ran_at, args.report)
        else:
            logging.info("Review aborted — no files written.")
            print("Aborted. No files written.")
        return

    write_outputs(result, file_stats, tasks_path, report_path, ran_at, args.report)


if __name__ == "__main__":
    main()
