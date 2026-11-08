# Proposal review and release approval

The dashboard builds a deterministic review pack from the extracted tender,
the accepted outline, the current draft, and the stored team match. It groups
findings as hard compliance failures, evidence gaps/human checks, and editorial
suggestions. Requirement rows retain the field-level source file, page and
passage when `extraction_provenance` can locate them. An absent or ambiguous
requirement stays unknown.

The existing quality score remains a drafting diagnostic. It is not a win
probability and does not establish compliance. Release-ready is a separate
human decision. The reviewer must check the tender instructions and eligibility,
verify any required attachments and external envelopes, and inspect the final
rendered Word/PDF. Unfilled team roles, unsupported factual claims,
unresolved placeholders, missed mandatory sections, and measurable format
failures block approval.

Human section edits are stored in `opportunity_processing.checkpoint` alongside
a bounded history of prior draft versions. Saving an edit invalidates the
grounding and quality checks; the edited text remains available for review and
export as a draft, but cannot be approved until it is grounded again. A
regeneration keeps the former draft as a downloadable version. Approval stores
the reviewer, time, acknowledgements, and a hash of the draft, extracted tender,
team match and tender content hash. A changed fingerprint makes the approval
stale. No new database migration is required because these fields use the
existing JSONB checkpoint.

Dashboard exports include Markdown and the existing formatted DOCX renderer.
Both are explicitly marked as internal drafts until human approval; internal
claim traceability is excluded from client-facing files. Final pagination and
rendering still require the reviewer’s Word/PDF inspection. If CV or evidence
records change without changing the stored team match, the dashboard cannot
detect that change automatically; rerun team matching before approval.
