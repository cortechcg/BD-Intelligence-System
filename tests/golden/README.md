# Golden evaluation set (Phase 0)

Hand-labeled fixtures for extraction and scoring evaluation. **20–40 items.**
No Airtable pull; no `.env` secrets. Client names are anonymized (`Client A`,
`UN-Agency A`). Geography that is typical of public tenders is kept.

Live Claude is **not** required. `pytest tests/ -q` must pass offline.

## Files

| Path | Role |
|---|---|
| `opportunities.json` | Canonical dataset (JSON array) |
| `loader.py` | Load + validate + `analysis_from_labels()` |
| `metrics.py` | Precision/recall, MAE, Jaccard; missing → `None` / skip |
| `grounding.py` | Labeled client/deadline/budget/geo/theme must appear in `document_text` |

## Schema (each item)

```json
{
  "id": "g-001",
  "source_kind": "past_proposal | synthetic_consultancy | synthetic_vacancy",
  "title": "anonymized assignment title",
  "document_text": "short reconstructed ToR-like text (untrusted data)",
  "labels": {
    "is_consultancy_contract": true,
    "client": "Client A or null",
    "donor": "Donor X or null",
    "deadline": "YYYY-MM-DD or null",
    "budget_usd": 45000,
    "thematic_areas": ["evaluation"],
    "geography": ["Somalia"],
    "language_requirements": [],
    "certifications": [],
    "submission_type": "FULL_PROPOSAL",
    "outcome": "WON | LOST | UNKNOWN"
  },
  "expected_recommendation": "BID | WATCH | NO-BID | null",
  "notes": "why this label, what is still UNKNOWN",
  "recorded_analyzer_json": { "opportunity": {}, "requirements": {}, "bid_analysis": {} }
}
```

Rules:

- Missing facts are `null` / `[]` / `UNKNOWN`. Never invent a deadline, budget, or win.
- `budget_usd` is only a positive number when the ToR-like text actually states it.
- Every non-null labeled client, deadline, budget, geography token, and thematic
  must appear in `document_text` (hyphen/space and light aliases allowed for
  themes). Vacancies must contain HR/staff language. Tests fail the set if a
  label is not grounded — recorded JSON that copies labels is not enough.
- `expected_recommendation` is null for staff vacancies: the boolean gate, not FIT, should stop them.
- `recorded_analyzer_json` is a recorded extraction object for the **offline parse harness**. It is not a live LLM result. Extra keys and comma-formatted budget strings are allowed so parse/coerce is exercised.

## What the tests measure vs what they do not

| Measured offline | Not measured (needs a live LLM pass later) |
|---|---|
| `parse_analysis_payload` / `analyze_rfp` with mocked `complete()` vs labels | Claude extraction accuracy on real ToRs |
| `is_consultancy_contract` precision/recall on recorded JSON | Calibrated P(win) / Brier score (Phase 6: sample too small to trust; 0 LOST) |
| Budget MAE and null agreement | Retrieval / RAG quality |
| Scorer recommendation vs hand-labeled expected band | Won/Lost outcomes (all UNKNOWN here) |

## Class design

Positive class = firm consultancy (mostly anonymized past proposals under
`data/proposals/`). Negative class = synthetic staff vacancies with HR language
so the boolean gate has both classes. Outcomes are `UNKNOWN` because Airtable
was not read.
