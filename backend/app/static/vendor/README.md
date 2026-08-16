# Vendored frontend assets

Prebuilt single files, downloaded once and committed. There is no npm, no
bundler and no build step: the app serves these bytes exactly as downloaded.

## Why they live here

`the project's constraints` states a hard constraint: no internet dependency at runtime, apart
from the bank-sync calls (Plaid / Pluggy). Loading Tailwind, HTMX, Alpine and
Chart.js from public CDNs broke that: with the house offline, every page
rendered unstyled and dead.

About Tailwind's "cdn.tailwindcss.com should not be used in production" console
warning: that line is an unconditional `console.warn` compiled into the Play CDN
bundle, so self-hosting alone does not silence it. The three templates that load
Tailwind therefore filter that one message around the script tag and restore
`console.warn` immediately after, which keeps the vendored file byte-identical to
upstream (checksums stay verifiable) and leaves every other warning visible.

## What each file is

| File | What it is | Version | Upstream URL |
|---|---|---|---|
| `tailwind-3.4.17.js` | Tailwind Play CDN, the in-browser JIT compiler (utilities are generated at page load, so `tailwind.config = {...}` in the templates keeps working) | 3.4.17 | `https://cdn.tailwindcss.com/3.4.17` |
| `htmx-1.9.12.min.js` | HTMX, minified dist build | 1.9.12 | `https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js` |
| `alpine-3.14.1.min.js` | Alpine.js, the CDN (auto-start) build, loaded with `defer` | 3.14.1 | `https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js` |
| `chart-4.4.4.min.js` | Chart.js UMD build (exposes the global `Chart`) | 4.4.4 | `https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js` |

`tailwind.config` is set inline in `templates/base.html`, `templates/phone/base.html`
and `templates/login.html`, immediately after the Tailwind tag. That ordering is
load-bearing for class-based dark mode: keep the config script after the vendor
script and before anything that renders.

## Cache busting

The version is in the filename, so a new version is a new URL and no `?v=`
query string is needed (the app-owned CSS still uses `?v=` because its name
never changes).

## How to update one

1. Download the pinned upstream URL for the new version (Tailwind's CDN root
   redirects to the current release, e.g. `https://cdn.tailwindcss.com` served
   3.4.17; prefer the explicit versioned URL).
2. Save it here under a filename carrying the new version.
3. Point every template at the new filename
   (`grep -rn 'static/vendor' backend/app/templates/`).
4. Delete the old file and commit both in the same change.
5. Run the suite: `backend/tests/test_no_external_assets.py` fails if a
   template ever points a script or stylesheet tag back at a CDN.

## Not vendored

`templates/connections.html` and `templates/phone/connections.html` load the
Plaid Link and Pluggy Connect widgets from their vendors' CDNs. Those are the
bank-sync integrations the constraint already exempts: each widget must match
the provider's live backend, and both are useless offline anyway. Every other
page of the app renders and works with no network at all.

## Licences

Each library keeps its upstream licence next to it as `<name>-<version>.LICENSE.txt`:

| File | Licence | Notes |
|---|---|---|
| `alpine-3.14.1.min.js` | MIT | notice not embedded in the minified build, hence the file beside it |
| `htmx-1.9.12.min.js` | 0BSD | notice not embedded in the minified build |
| `chart-4.4.4.min.js` | MIT | banner also embedded at the top of the file |
| `tailwind-3.4.17.js` | MIT | banner also embedded in the file |

MIT requires the copyright and permission notice to travel with any
redistribution, and vendoring is redistribution, so shipping these minified
files without their notices would be a licence defect. When you update a
library, replace its licence file in the same commit.
