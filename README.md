# Odisena Console

An offline-capable, installable **Progressive Web App** that serves as an
engineering command center for OpenTelemetry (OTel) performance work, RDS
Postgres migration runbooks, and IAM security tooling. It indexes and renders
engineering **runbooks** (markdown) and lets you browse and download
**artifacts** (code, YAML workflows, zips, dashboards, images), grouped by
work **session** and **domain**.

## Highlights

- **Zero dependencies, zero build.** Pure static HTML/CSS/JS. The markdown
  parser (`marked.min.js`) is bundled — no CDN, no external network calls.
- **Host-agnostic.** All asset references are relative; runs from a domain
  root, a sub-path, or an object store. No Perplexity sandbox / `/computer/a`
  assumptions.
- **Storage-free.** No cookies, `localStorage`, `sessionStorage`, or
  `indexedDB`. All UI state is in memory, so it works in sandboxed/restricted
  browser contexts.
- **Installable + offline.** PWA manifest + service worker cache the shell and
  data for offline use (same-origin only).
- **Light/dark themes**, mobile-first layout, tab-bar navigation, and
  hash-based routing (survives back-swipe on iOS Safari).

## Features

- **Home** — live counts, domain cards, quick-access shortcuts, recent sessions.
- **Runbooks** — searchable, category-filterable list; in-app markdown reader.
- **Artifacts** — searchable, filterable, direct-download list.
- **Sessions** — browse work grouped by session.
- **Ops** — scheduled tasks, dashboards, and the GitHub workflows built.

## Data model

Everything the app displays comes from `catalog.json`:

- `categories` — domain taxonomy (`otel`, `rds`, `iam`) with colors + counts.
- `sessions` — work sessions with titles and file lists.
- `runbooks` — markdown docs (rendered in-app), each with a relative `path`.
- `artifacts` — downloadable files, each with a relative `path`, `ext`, `size`.
- `stats` — totals shown on the home screen.

## Run locally

Any static file server works. For example:

```bash
cd odisena-console-hosted
python3 -m http.server 8000
# open http://localhost:8000
```

The service worker registers on `http://localhost` and any HTTPS origin.

## Deploy

See **[DEPLOYMENT.md](./DEPLOYMENT.md)** for step-by-step instructions for
Vercel, Netlify, Cloudflare Pages, AWS S3 + CloudFront, and generic
static hosts / nginx. Host config files (`vercel.json`, `netlify.toml`,
`_headers`) are included and set sensible cache + security headers.

## Updating content

Add/replace files under `runbooks/` or `artifacts/`, update the matching entry
in `catalog.json` (relative `path`), bump the `CACHE` constant in `sw.js`, and
re-deploy.
