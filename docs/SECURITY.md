# Security (as implemented)

Threat model: a **local** process that fetches public tender pages/PDFs/emails and sends them to Claude. Humans receive drafts. There is no public HTTP API.

## Secrets

- `.env` and every `.env.*` except `.env.example` are gitignored (and
  dockerignored). `tests/test_secret_scan.py::test_env_files_are_ignored`
  fails if that ever changes.
- `.env.example` is **placeholders only**.
- Logs must not print full API keys. Analyzer 401 messages use last-4 only.
- Do not dump full ToR text into structured log lines (`utils/observability.log_stage` truncates field values).

### Secret scanning

`scripts/secret_scan.py` (stdlib only) knows the formats this project uses:
Anthropic, OpenAI, Airtable PAT, Supabase JWT / `sb_secret_` / `sbp_`,
Resend, Google OAuth client secret, GitHub tokens, private key blocks,
Postgres URLs with a password, and any `*_PASSWORD` / `*_SECRET` /
`*_API_KEY` / `*_SERVICE_KEY` assignment with a non-placeholder value. It
reports rule, location and a sha256 fingerprint. It never prints a value.

| Where | Command |
|---|---|
| pre-commit (per clone: `git config core.hooksPath .githooks`) | `scripts/secret_scan.py --staged` |
| CI job `secret-scan` | `--tree`, `--history` (all refs, every blob), then gitleaks `detect --redact` over full history |

A deliberate fake in a test is built at runtime, or its line carries the
comment `secret-scan: allow`.

### 2026-10-28 exposure (public repository)

`cortechcg/BD-Intelligence-System` is **public**. Before the 2026-10-28
scrub, history contained `.env` (added in the first commit, 2e34fa6) and a
`.env.example` with live values (from c2f6b2f / 39a370c onward). Values
found, by name: `SUPABASE_SERVICE_KEY` (two distinct values),
`ANTHROPIC_API_KEY` (two), `OPENAI_API_KEY` (two), `AIRTABLE_API_KEY` (two
PATs), `RESEND_API_KEY`, `GMAIL_APP_PASSWORD`, `IMAP_PASSWORD`. On
2026-10-28 the Airtable PAT, Supabase service key, Gmail app password and
IMAP password in the working `.env` were **still the leaked values**.

A history rewrite removes the values from future clones only. Anyone who
cloned or forked before it, and GitHub's cached commit views, still have
them. Every value above must be treated as compromised and rotated at the
provider; the rewrite is hygiene, not remediation.
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

`.github/workflows/ci.yml` runs the secret scan (above), then `pip-audit -r requirements.txt` then pytest
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

`log_agent_action` never raises. The pipeline does not call Airtable.

## What we did not build

No WAF, no DLP product, no OS-level Playwright/parser sandbox. `pip-audit`
is in CI; current `requirements.txt` is clean as of the 2026-09-15 re-scan.
