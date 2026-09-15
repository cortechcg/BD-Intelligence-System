# ADR 008 — Named past-work claim grounding (Phase 4)

## Status

Accepted

## Context

This is Phase 4 of the S-Tier BD Intelligence OS upgrade. It implements the
named past-work half of ADR 003. It does **not** start Phase 5 (competitor
or relationship intelligence), a win model, or a full source → page → chunk
→ every-sentence proposal graph.

Scorecard / remaining-debt / provenance lines this ADR implements (quoted,
not paraphrased):

- §19 Proposal Intelligence **6/10**: “Tender/past-proposal context and
  compliance output; no claim verification.”
- §16: “Field-level ToR provenance exists; proposal-claim provenance does
  not.”
- ADR 003 Decision: “After drafting, `intelligence/grounding.py` matches
  named past-assignment claims to those chunks. Unsupported named
  clients/projects are tagged `[NOT VERIFIED]` in the draft. Generic boasts
  without an entity are `INSUFFICIENT EVIDENCE` in the report only.”
- ADR 003 Decision: “Writers must mark unsupported claims `[NOT VERIFIED]`
  or `[INSUFFICIENT EVIDENCE]`.”
- ADR 005: “ADR 003 (provenance philosophy) remains in force: … past-work
  claims stay a retrieved-chunk problem for `intelligence/grounding.py`.
  This ADR does **not** replace ADR 003 with a claim graph. It adds
  extraction-field → source-chunk pointers so a later Phase 4 claim
  verifier has somewhere real to look.”
- `docs/CURRENT_STATE.md` (pre-Phase 4): “there is still no full provenance
  chain from source page to **generated proposal claim**. … Phase 4
  (proposal claim verification) was not started.”

Phase 1 (ADR 005) closed ToR field → source chunk. Phase 4 closes generated
**named past-work** claim → retrieved past-proposal chunk. These are
different loops. Field-level ToR provenance stays in
`intelligence/extraction_provenance.py`.

Two pre-existing `tests/test_grounding.py` failures were the real definition
of this work, not noise:

1. `test_named_past_client_in_chunk_is_verified` — a named client present in
   a retrieved `proposal_embeddings` chunk must resolve to that chunk
   (`proposal:recARCH`), not to `profile:cortech` just because the name is
   also on `CORTECH_PROFILE` KEY CLIENTS.
2. `test_invented_past_client_is_not_verified_and_annotated` — an invented
   named client must stay in the draft tagged `[NOT VERIFIED]`, not be
   silently deleted (the redact path left `[INSUFFICIENT EVIDENCE]` as the
   whole section).

Production `ground_sections()` had been stripping unverified sentences.
That contradicts ADR 003. This phase makes production match ADR 003 and
those tests. `redact_unverified_claims` is not the default path.

## Decision

1. **One verifier, already named.** Extend `intelligence/grounding.py`. Do
   not add a parallel LLM “confirm this claim” step. Retrieval +
   deterministic matching is official. Claude drafts; code grounds.

2. **Named past-work only.** A sentence is in scope when it carries a past-
   assignment marker (“previously”, “has done”, “track record”, “has
   delivered”, …) or a named-expert claim. Methodology, this-tender facts,
   win-strategy notes, and ToR restatements are not a full claim graph.
   Status vocabulary stays ADR 003: `VERIFIED` / `NOT VERIFIED` /
   `INSUFFICIENT EVIDENCE`.

3. **VERIFIED requires a retrieved (or static-fallback) assignment chunk.**
   Official sources for past-work names: `proposal_embeddings` first, then
   Airtable `PAST_PROPOSALS` rows only if they were actually supplied as
   `past_matches`, then the static `CORTECH_PAST_WORK` list when retrieval
   returned nothing. `CORTECH_PROFILE` KEY CLIENTS is **not** assignment
   evidence. Empty embeddings fail-open at search; claims still get
   `NOT VERIFIED` unless a chunk remains. When both profile and a proposal
   chunk mention the same client, `chunk_id` is the proposal chunk.

4. **Missing chunk is visible.** Unsupported named clients/projects are
   tagged `[NOT VERIFIED]` inline so a human reviewer sees the sentence.
   Generic boasts without a named entity are `INSUFFICIENT EVIDENCE` in
   `claim_grounding` only — the prose is not rewritten. Do not silently
   pass. Do not invent past work.

5. **Wire-up stays in `proposal_writer._attach_claim_grounding`.** Same
   retrieved matches the writer was given, plus the static list only when
   that list is empty. Tender/proposal text in LLM prompts stays inside
   `wrap_untrusted()`. Nothing submits to a client. Models remain
   `CLAUDE_MODEL` / `CLAUDE_MODEL_PROPOSAL`. Financial figures stay under
   `money_scrub`.

6. **Failure modes do not crash and do not silent-accept.** `None` /
   non-dict sections, malformed `past_matches`, empty retrieval, and
   adversarial claim text return a `claim_grounding` report. Named invented
   clients are still flagged.

## Consequences

- Reviewers will see `[NOT VERIFIED]` in drafts. That is the product, not a
  defect. The review email lists those claims; it must not say they were
  stripped if they were tagged in place.
- This is not 10/10 Proposal Intelligence: there is still no graph from
  every sentence to a source page, and assignment *details* (dates, methods)
  are not proven beyond named-entity presence in a chunk.
- Golden-set extraction/scoring is a different loop and must not move.
- Phase 5 (competitors, relationships) is not started here.

Pytest on 2026-09-15 (`~/cortech-bd-agent/cortech/bin/python -m pytest tests/ -q`):

```text
284 passed in 14.46s
```
