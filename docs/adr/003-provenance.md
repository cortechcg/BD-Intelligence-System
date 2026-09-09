# ADR 003 — Provenance and evidence labels

## Status

Accepted

## Context

Drafts and scores that invent clients, CVs, or confidence destroy trust. The corpus of past proposals and CVs is real; anything not in that corpus is not a fact.

## Decision

- Retrieved past-work chunks keep source metadata (title, client, year, location, similarity, table name, chunk_id).
- After drafting, `intelligence/grounding.py` matches named past-assignment claims to those chunks. Unsupported named clients/projects are tagged `[NOT VERIFIED]` in the draft. Generic boasts without an entity are `INSUFFICIENT EVIDENCE` in the report only.
- Writers must mark unsupported claims `[NOT VERIFIED]` or `[INSUFFICIENT EVIDENCE]`.
- Scoring factors are VERIFIED / INFERRED / UNKNOWN. UNKNOWN is dropped from weighted averages, not filled with a default “probably 50”.
- Capability matching does not infer education or certifications missing from CV metadata.
- Compliance matrix statuses are SATISFIED / PARTIAL / MISSING / UNKNOWN. Award criteria (`evaluation_criteria`) are not mixed with `assignment_evaluation_framework`.

## Consequences

Many cells will be UNKNOWN. That is the honest output. A dashboard that needs green checks everywhere is not this system.
