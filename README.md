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

Production is **GitHub Pages**, published from the `main` branch root — merge to
`main` and Pages rebuilds automatically. The live site is served at
**https://console.odisena.com/** via the committed `CNAME` file, which is the
Pages custom-domain binding; keep it (and `.nojekyll`) intact. Because Pages
cannot set HTTP response headers, security is delivered via a meta
Content-Security-Policy, a referrer meta, and the HTTPS/HSTS/nosniff that Pages
provides automatically; clickjacking protection is a documented gap on this host.
DNS for `console.odisena.com` is managed outside this repo — retaining the
`CNAME` file changes no DNS records.

See **[DEPLOYMENT.md](./DEPLOYMENT.md)** for the full posture, the rollback
procedure (`git revert` + merge), service-worker cache-key rules, and notes on
portability to other static hosts.

## Updating content

Add/replace files under `runbooks/` or `artifacts/`, update the matching entry
in `catalog.json` (relative `path`), bump the `CACHE` constant in `sw.js`, and
re-deploy.

## Author

Created by **Ian Dansereau** (Ian Winfield Dansereau) — Governance Lead, Value
Creation Office at Brightspeed, working in telecom & financial oversight and
data governance. Background spans telecommunications engineering, financial
consulting and dispute advisory, wireless and structured cabling design, quality
control, field testing & commissioning, manufacturing, mechanics, and
prototyping. Mechanical engineering, Georgia Institute of Technology. Based in
Bethesda, Maryland.

- LinkedIn: <https://www.linkedin.com/in/ianwdansereau>
