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

`utils.urls.assert_public_http_url()` blocks non-http(s), localhost, link-local/metadata IPs, RFC1918 literals, and userinfo. Applied in `download_document`, `fetch_and_extract`, Playwright fallback, RSS feed fetch, and followed PDF links. `download_document()` follows redirects manually and applies the same public-URL check to every redirect target before requesting it. It accepts at most five redirects and 25 MiB by default; both are environment-configurable. Transient network/timeouts and selected 408/425/429/5xx responses retry at most twice with 1s/2s backoff. Unsafe URLs, oversize documents, and other client errors do not retry.

TLS certificates are always verified. A source with a broken certificate fails visibly rather than triggering an insecure retry.

Download GETs are DNS-pinned (`utils/dns_pinned_http.py`): the hostname is
resolved once, any non-public address in the answer is rejected, and the
client connects to that IP with the original Host header and TLS SNI. httpx
is not allowed to re-resolve between the check and connect on that path.

Not solved: Playwright still performs its own DNS for page subresources
(those hops still pass `assert_public_http_url` / `assert_safe_redirect` in
the request guard, which is a check-then-fetch TOCTOU, not a pin-IP
transport). Document parsers are in-process.

## Playwright process limits

`utils/browser_security.py` sets page default/navigation timeouts and Chromium
flags (`--js-flags=--max-old-space-size`, `--renderer-process-limit=1`,
`--disable-dev-shm-usage`). `--no-sandbox` is still required on this host.
This is **not** a kernel namespace, cgroup memory kill, or gVisor jail.

## CI dependency scan

`.github/workflows/ci.yml` runs `pip-audit -r requirements.txt` then pytest
excluding `tests/test_live_supabase_stages.py` (CI must not write hosted
`opportunity_processing`).
A 2026-09-15 scan reported PYSEC findings in `cryptography` 49.0.0,
`h2` 4.3.0, `pillow` 12.2.0, and `pytest` 8.4.2. Those were bumped to
`cryptography==50.0.0`, `h2==4.4.1`, `pillow==12.3.0`, `pytest==9.0.3`.
Re-scan: no known vulnerabilities. Offline suite 2026-09-17: 366 passed,
0 failed, 0 skipped (hosted live tests not run).

## Outbound review email

Titles, client names, source URLs, CV matches, model-generated prose, and
budget explanations are HTML-escaped before insertion into an HTML email.
Email subjects strip CR/LF characters. This prevents scraped or model-returned
text from altering the rendered review email or injecting a second header.

## Path traversal

Storage keys use `safe_filename()` (basename only) in the downloader and `store_document`.

## Airtable

`typecast=True` on create and opportunity update. `log_agent_action` never raises. Fail-fast retries (not 21-minute backoff).

## What we did not build

No WAF, no DLP product, no OS-level Playwright/parser sandbox. `pip-audit`
is in CI; current `requirements.txt` is clean as of the 2026-09-15 re-scan.
