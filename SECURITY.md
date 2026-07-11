# Security Policy

The Odisena Console is a **fully static** Progressive Web App served over
HTTPS by GitHub Pages at **https://console.odisena.com/**. It has no backend,
no build step, no secrets, and makes no external network calls at runtime
(the markdown parser is bundled; there are no CDNs). This document covers the
security posture of the site and how to report issues.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public
issue for anything exploitable.

- Preferred: GitHub **Security advisories** →
  *Report a vulnerability* on this repository
  (`Security` tab → `Advisories` → `Report a vulnerability`).
- Include: affected URL/asset, a description, reproduction steps, and impact.

We aim to acknowledge reports within a few business days. Because the site is
static and storage-free, most classes of server-side vulnerability do not
apply; the highest-value reports concern the custom-domain binding, TLS,
content-injection through `catalog.json`/runbooks, or the service worker.

## Threat model & guarantees

- **No data at rest in the browser.** No cookies, `localStorage`,
  `sessionStorage`, or `indexedDB`. All UI state is in memory.
- **Same-origin only.** The service worker (`sw.js`) caches same-origin assets
  and never proxies cross-origin requests.
- **No third-party runtime code.** `marked.min.js` is vendored; there is no
  CDN, analytics, or telemetry.
- **Security headers** are set by the host configs (`_headers`, `netlify.toml`,
  `vercel.json`) for hosts that honour them. GitHub Pages does not apply custom
  headers, so the site is written to be safe without them.

## Custom-domain & DNS hardening

The custom domain is the most security-sensitive part of a static Pages site:
a lost domain binding, a dangling DNS record, or an apex that silently starts
serving the Pages site are all takeover/confusion risks. The repository ships
a **domain preflight** to catch these:

```bash
# Offline (CI-safe): validate the committed CNAME + .nojekyll.
python3 .github/scripts/preflight_domain.py

# Live (read-only): also verify DNS, TLS, HTTP, and apex isolation.
python3 .github/scripts/preflight_domain.py --live
```

What it enforces:

- **`CNAME` integrity** — exactly one bare hostname (`console.odisena.com`),
  no scheme, path, or trailing slash. This file *is* the Pages custom-domain
  binding; losing it drops the site back to `*.github.io`.
- **`.nojekyll` present** — files are served verbatim.
- **DNS** — `console.odisena.com` is a `CNAME` to `iwdansereau-ops.github.io`
  and resolves to GitHub Pages' published A/AAAA address set only.
- **Apex isolation** — the naked apex (`odisena.com`) must **not** point at the
  Pages address set. The console lives on its own subdomain; the apex is hosted
  independently. This prevents the apex from unintentionally serving the
  console and reduces takeover surface.
- **TLS** — the served certificate's SAN covers the host.
- **HTTP** — the site responds (200/redirect) over HTTPS.
- **Founder vanity aliases** — optional; reported as present/absent. **Absent
  aliases are expected and are never auto-created.**

The preflight is strictly **read-only**: it performs DNS queries, a TLS
handshake, and HEAD requests. It never mutates DNS, Pages settings, or
repository state. Run it before and after any change that touches `CNAME`,
`.nojekyll`, or DNS.

## Supported versions

Only the currently deployed `main` branch is supported. There are no released
versions or backports; fixes ship by merging to `main`, which GitHub Pages
republishes automatically.
