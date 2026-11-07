# Configurable procurement sources

The dashboard **Sources** screen stores custom sources in Supabase and sends
supported results through the normal scheduled discovery path. A discovered
opportunity enters the existing queue; extraction, scoring, and drafting still
run through the usual dashboard action and opportunity pipeline.

## Install

1. Apply `supabase_migration_procurement_sources.sql` to the Supabase project
used by this application. It creates the source registry and atomic check
leases, enables row-level security, grants access only to the server-side
`service_role`, and adds optional source metadata to `opportunities_cache`.
2. Deploy the application code and restart both the dashboard and the existing
   scheduled discovery process. No new environment variables or dependencies
   are required.
3. Sign in with an account allowed by the existing dashboard access policy and
   open `/sources`.

The migration is intentionally not run by this code change. Applying it to a
production Supabase project and deploying the branch are separate operator
actions that require approval.

## Add and activate a source

1. Enter a name and a public HTTP(S) feed or listing URL. The app normalizes
   the URL and rejects duplicate URLs, embedded credentials, nonstandard
   ports, and literal private or reserved addresses.
2. The app performs one bounded public check. RSS and Atom feeds are supported.
   Static listing pages are supported only when procurement notices have a
   reliable, repeatable public structure. The source screen shows up to five
   preview entries.
3. Review the preview, then choose **Validate and enable**. A successful source
   is included automatically in the next scheduled discovery run. **Test
   source** checks and previews the source without adding an opportunity or
   starting a dashboard job. **Pause** excludes a custom source from scheduled
   checks; enabling it again restores discovery.

Unsupported pages stay inactive. The dashboard identifies confirmed
authentication requirements, access restrictions, CAPTCHA blocks, unsupported
pages, adapter requirements, and temporary network/provider failures
separately. The app does not sign in,
bypass a CAPTCHA, execute page scripts, crawl a domain, or fetch a listing's
attachments during source validation.

## Bounds and recovery

- Each check reads one feed or listing page, follows at most two validated
  redirects, and reads at most 2 MiB.
- Feed parsing considers at most 40 entries; public HTML extraction considers
  at most 20 listings.
- Each source check has a 9-second total fetch deadline. A scheduled run checks
  at most eight custom sources and stops starting work after 45 seconds.
- Checks use the DNS-pinned downloader. Every hop is checked and connected to a
  validated public address. Discovered links are syntax-checked before they
  enter the queue; later opportunity/document fetches use the existing pinned
  download path. JavaScript/browser fallback is disabled for custom-source
  opportunities; their HTML and attachments are handled by static parsing and
  the pinned downloader, so fetched page scripts are not executed.
- A database lease prevents concurrent checks for the same source. Pausing a
  source during a fetch discards that run's results. The database also caps
  custom-source checks at four concurrent runs across dashboard and worker
  replicas. Failed checks retain the
  prior preview and successful-check timestamp, record a separate attempt,
  and use increasing scheduled retry delays up to 24 hours. A manual test
  action is the recovery control and is rate-limited to one attempt every 60
  seconds per source.
- Source health counts increase only when a result creates a new discovery
  cache row. Repeated results keep the same source identity; lot and addendum
  variants receive distinct internal discovery identities while retaining the
  original public source URL and identifiers in `source_metadata`.

Existing built-in adapters remain enabled and run through their existing
parsers. Their attempt, success, discovered-count, and error fields are shown
when the source registry migration is installed.

## Manual verification checklist

- Sign in as an allowed user and open `/sources`; confirm built-in adapters are
  present with health fields.
- Add a public RSS/Atom source in a staging project. Confirm the preview,
  inactive initial state, normalized URL, and duplicate rejection.
- Enable the source, run the existing scheduled discovery command once, and
  confirm a new opportunity appears in the dashboard queue with its original
  source URL, reference, lot, or addendum when supplied by the feed.
- Run discovery again and confirm the cache row and source discovered count do
  not duplicate. Pause the source, run discovery, and confirm it is not checked;
  enable it and confirm it is checked on the next run.
- Test an unsupported, authentication-required, or CAPTCHA-blocked page and
  confirm it remains inactive with the specific reason shown.
- Submit an invalid CSRF token to each source mutation and confirm HTTP 403;
  make the same requests without a session and confirm the dashboard auth
  middleware rejects them.
