# Session 6 — Bible Daily Log + Three Modal Replacements

## Feature 1 — Daily Bible Log (backend + frontend + brain dump)

### New Model: DailyBibleLog
- id
- date (date, not unique — multiple entries per day allowed)
- book (string, e.g. "Proverbs")
- chapter (int)
- notes (text, nullable)
- created_at (timestamp)

### New Endpoints
- POST   /bible-log              — create entry
- GET    /bible-log/today        — today's entries
- GET    /bible-log/recent?days=30 — last N days grouped by date

### Bible Plan Page Additions
Below existing ReadingPlan cards, add "TODAY'S READING" section:
- Quick add form (always visible, compact): [Book] [Chapter] [Notes] [+ Log]
- Below form: today's entries as simple list (book, chapter, notes, delete button)
- Below today: collapsible "Reading History" section
  - Fetches /bible-log/recent?days=30
  - Groups entries by date, newest first
  - Each entry: date header, then book + chapter + notes

### Brain Dump Integration
Add bible_log to AI extraction schema in openai_client.py:
```
bible_log: [{ book: string, chapter: int, notes: string | null }]
```
Example in prompt:
- Input: "read Proverbs chapter 12 this morning"
- Expected: bible_log: [{"book": "Proverbs", "chapter": 12, "notes": null}]
- NOT reading_updates (that's for fiction/non-fiction queue books)

In apply_parse_updates():
- For each entry, create DailyBibleLog row for today
- Check if active ReadingPlan.current_book matches logged book (case-insensitive)
- If match: update ReadingPlan.current_chapter to logged chapter
- If no match: just log, don't touch any plan
- Return bible_log_entries_created count in updates response

### Regression Tests
1. "read Proverbs 12" → DailyBibleLog row created, no ReadingQueue book affected, no Todo
2. "read Proverbs 12" with active plan current_book="Proverbs" → log + plan chapter updated to 12
3. "read Proverbs 12" with active plan current_book="Matthew" → log only, plan NOT updated

---

## Feature 2 — Subtopic Add Modal

Replace window.prompt() in arAddSubtopic() with a proper UI modal.

Modal contents:
- Title: "Add Subtopic"
- Field: Subtopic name (text input, autofocused)
- Buttons: [Cancel] [Add]

Behaviour:
- Opens on "+ Add Subtopic" click
- Enter submits, Escape cancels
- POST /topics/{id}/subtopics with name, mastery_level=0
- On success: close modal, refresh subtopic list
- Style: match existing modal pattern in codebase

---

## Feature 3 — Bible Plan Edit Modal

Replace window.prompt() in editReadingPlan() with a proper UI modal.

Modal contents:
- Title: "Edit Reading Plan"
- Fields: Plan name, Current book, Current chapter (number), Daily target chapters (number), Target completion date (date, optional), Notes (textarea, optional)
- Buttons: [Cancel] [Save]

Behaviour:
- Pre-fills all fields with current plan values
- PATCH /reading-plans/{id} on save
- Close modal on success, refresh cards
- Escape to cancel

---

## Feature 4 — Trading Desk Inline Validation

Remove all HTML required attributes. Replace with JS inline validation.

Required fields:
- Backtest: Pair, Direction, R-multiple, Adherence, Outcome
- Live trade: Pair, Direction, R-multiple, Adherence, Outcome, Account, Risk %, Net P/L

Optional fields:
- Both: Date (auto-fills today), Time, Entry Reason
- Live: Rule Broken (defaults to No)

Validation behaviour:
- Remove HTML required attributes
- On Log click: check required fields in JS
- Empty required field: red border + small error message below
- Do NOT submit if any required field empty
- Clear error state on user input
- On success: submit normally, clear all error states

Style: red border (#e74c3c), small italic error text below field.

---

## Execution Order
1. Feature 1 backend (model + endpoints + brain dump)
2. Feature 1 frontend (Bible Plan page additions)
3. Feature 1 tests
4. Feature 2 (subtopic modal — frontend only)
5. Feature 3 (Bible plan edit modal — frontend only)
6. Feature 4 (trading validation — frontend only)

Run pytest after Feature 1 backend before moving to frontend.

## Git
Diff stat after each feature. No commit until all four done. Full pytest at end.

## End-of-session audit (REQUIRED)
Re-read this file. For each feature confirm:
- Implementation location (file/line)
- Tests added (Feature 1 only)
- Any spec items modified or skipped

## DO NOT
- Add swipe gestures or complex animations to modals
- Add image/file upload to Bible log
- Add set-by-set tracking to trading
- Touch Health, Aether Command, or any other module
- Add AI suggestions to any of these features
