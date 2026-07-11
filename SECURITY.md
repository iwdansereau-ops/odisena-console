# Security Policy

The Odisena Console is a **fully static** Progressive Web App served by GitHub
Pages at <https://console.odisena.com/>. It has no backend, no build step, no
secrets, and makes no external network calls: every asset is bundled and served
same-origin, and all UI state is held in memory (no cookies, `localStorage`,
`sessionStorage`, or `indexedDB`). This narrows the attack surface to the static
content itself, the hosting/domain configuration, and the client-side rendering
of catalog data.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public
issue for anything exploitable.

- Preferred: open a [GitHub private security advisory](https://github.com/iwdansereau-ops/odisena-console/security/advisories/new)
  ("Report a vulnerability").
- Alternatively: email the repository owner (`iwdansereau-ops`).

Please include the affected URL/path, reproduction steps, and impact. We aim to
acknowledge reports within **5 business days** and to agree on a remediation
timeline after triage. We support coordinated disclosure and will credit
reporters who wish to be named once a fix has shipped.

## Supported versions

Only the currently deployed `main` branch is supported. There are no long-lived
release branches; fixes ship by merging to `main`, which GitHub Pages rebuilds
automatically.

## Scope

In scope:

- Cross-site scripting or content-injection via `catalog.json`, runbook
  markdown, or artifact rendering in the app shell (`index.html`, `app.js`,
  `marked.min.js`).
- Service-worker cache-poisoning or stale-content issues in `sw.js`.
- Weakened HTTP security headers in `_headers`, `netlify.toml`, or
  `vercel.json`.
- Loss or misconfiguration of the GitHub Pages custom-domain binding (see
  below).

Out of scope:

- Denial-of-service, volumetric, or automated scanning against the hosted site.
- Findings in third-party hosting infrastructure (GitHub Pages, DNS provider)
  themselves — report those to the respective vendor.
- Missing headers on downloadable artifacts under `artifacts/`, which are
  payloads, not the app shell.

## Custom-domain integrity (do not remove)

The custom-domain publish of `console.odisena.com` depends on load-bearing
committed files. Removing or corrupting any of them silently drops the site
back to the `*.github.io` default and disables HTTPS enforcement:

- **`CNAME`** — must contain exactly `console.odisena.com` (a bare hostname:
  no scheme, no trailing slash, no extra whitespace or lines).
- **`.nojekyll`** — disables Jekyll processing so all files are served
  verbatim.
- **`index.html`** / **`404.html`** — the Pages entry document and custom
  not-found page.

These invariants are enforced automatically by
[`.github/scripts/pages_preflight.py`](.github/scripts/pages_preflight.py),
which runs in CI (offline) and can additionally cross-check the live Pages API
(`--live`, read-only). Never bypass this check to land a change that alters the
domain binding, and never edit DNS from this repository.

## Handling of data

The console displays only non-sensitive engineering runbooks and artifacts that
are already committed to this public repository. Do not commit secrets,
credentials, tokens, or personal data to `catalog.json`, `runbooks/`, or
`artifacts/`.
