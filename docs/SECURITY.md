# Security (as implemented)

Threat model: a **local** process that fetches public tender pages/PDFs/emails and sends them to Claude. Humans receive drafts. There is no public HTTP API.

## Secrets

- `.env` is gitignored and must not be committed.
- `.env.example` is **placeholders only**. It previously contained live-looking credentials; that was a real defect and has been replaced.
- Logs must not print full API keys. Analyzer 401 messages still use last-4 only.
- Do not dump full ToR text into structured log lines (`utils/observability.log_stage` truncates field values).

## Untrusted document content

Scraped PDFs, HTML, Google Drive files, and newsletter blurbs are **data**.

- `utils/untrusted.wrap_untrusted()` wraps analyzer (and RSS deadline) prompts.
- Delimiter injection inside a PDF cannot close the untrusted block.
- `intelligence/tender_reader.py` states the tender pack cannot override writing rules.
- Tests in `tests/test_prompt_injection.py` assert “ignore previous instructions” stays inside the data block.

This is prompt-injection *mitigation*, not a proof against every jailbreak. A determined document can still bias extraction. The hybrid scorer ignores the LLM's numeric score, which removes one injection payoff (forcing BID/100).

## SSRF / downloads

`utils.urls.assert_public_http_url()` blocks non-http(s), localhost, link-local/metadata IPs, RFC1918 literals, and userinfo. Applied in `download_document`, `fetch_and_extract`, Playwright fallback, RSS feed fetch, and followed PDF links.

Not solved: DNS rebinding (resolve to public, then to 127.0.0.1). Would need a pin-IP transport.

SSL verification may still be disabled on retry for broken procurement-site certificates (pre-existing, logged).

## Path traversal

Storage keys use `safe_filename()` (basename only) in the downloader and `store_document`.

## Airtable

`typecast=True` on create and opportunity update. `log_agent_action` never raises. Fail-fast retries (not 21-minute backoff).

## What we did not build

No WAF, no secret scanner CI, no sandbox for Playwright, no DLP product. Those would be theatre at this repo's size.
