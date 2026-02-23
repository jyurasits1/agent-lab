# inbox_triage

Reads plain-text files from `inbox/`, applies rule-based heuristics to extract
structured tasks, and writes the result to `out/tasks.json`.

No external dependencies — Python 3.10+ standard library only.

---

## Directory layout

```
agents/inbox_triage/
├── run.py          # main script
├── inbox/          # drop .txt files here
│   └── example.txt
├── out/            # created on first run
│   └── tasks.json
└── README.md
```

Logs are written to `logs/latest.log` at the repo root (created automatically).

---

## Usage

Run from the **repo root**:

```bash
# Normal run — writes out/tasks.json and logs/latest.log
python agents/inbox_triage/run.py

# Dry run — prints JSON to stdout, writes nothing
python agents/inbox_triage/run.py --dry-run
```

---

## Output schema

```json
{
  "tasks": [
    {
      "title":       "string  — first sentence / up to 90 chars",
      "summary":     "string  — full cleaned text, up to 300 chars",
      "priority":    1,        // 1 = high, 2 = medium, 3 = low
      "due_date":    "YYYY-MM-DD or null",
      "next_action": "string  — suggested immediate step",
      "tags":        ["bug", "frontend", ...]
    }
  ],
  "assumptions": ["string"],
  "questions":   ["string"]
}
```

### Priority heuristics

| Level | Signals |
|-------|---------|
| 1 — High   | "asap", "urgent", "customers are complaining", "production", "blocker" … |
| 2 — Medium | "soon", "this week", "deadline", "by end of", "fix asap" … |
| 3 — Low    | "low priority", "cosmetic", "when there's bandwidth", "no hard deadline" … |

Defaults to **2 (medium)** when no signal is found.

### Tag detection

Tags are assigned when keyword groups match the item text:
`bug`, `feature`, `docs`, `tech-debt`, `infra`, `frontend`, `backend`, `customer-facing`.

---

## Adding more inbox files

Drop any `.txt` file into `inbox/` and re-run the script. All `.txt` files are
processed and their tasks are merged into a single `tasks.json`.
