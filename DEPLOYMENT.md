# Deploying the Odisena Console

The Odisena Console is a **fully static** Progressive Web App. There is no
build step, no server-side code, and no external network dependency — every
asset (HTML, CSS, JS, the `marked` markdown parser, icons, runbooks, artifacts,
and `catalog.json`) is bundled in this directory.

**Production host: GitHub Pages.** The live site is published from the `main`
branch root and served at **https://console.odisena.com/** through the committed
`CNAME` file (see [Custom domain](#custom-domain-consoleodisenacom)). GitHub
Pages is the single canonical host; the previous Vercel / Netlify / Cloudflare
Pages / S3 config files (`vercel.json`, `netlify.toml`, `_headers`) have been
removed because Pages does not read them, so they described protections that
never actually applied in production. The app remains portable to any static
host (see [Portability](#portability-other-static-hosts)), but Pages is what
production assumptions are written against.

All references are relative, so the site works identically from a domain root,
a sub-path, or a static object store.

---

## Directory layout

```
.
├── index.html              # App shell (hash-routed SPA)
├── app.js                  # Client logic (deferred; registers the service worker)
├── styles.css              # Styles (light/dark via prefers-color-scheme)
├── marked.min.js           # Bundled minimal markdown parser (no CDN)
├── catalog.json            # Data index: sessions, runbooks, artifacts, stats
├── manifest.webmanifest    # PWA manifest
├── sw.js                   # Service worker (offline cache, same-origin only)
├── 404.html                # Static not-found fallback
├── robots.txt              # Crawler policy
├── CNAME                   # Pages custom-domain binding (console.odisena.com)
├── .nojekyll               # Disables Jekyll so files are served verbatim (Pages)
├── icons/                  # PWA + favicon assets
├── runbooks/               # Markdown runbooks rendered in-app
└── artifacts/              # Downloadable artifacts (code, YAML, zips, HTML, PNG)
```

---

## Security posture on GitHub Pages

GitHub Pages **cannot set custom HTTP response headers.** That constrains what
protections are available, so they are delivered in the ways Pages does support:

| Protection | How it is delivered | Notes |
| ---------- | ------------------- | ----- |
| HTTPS + HSTS | Automatic (Pages enforces HTTPS and sends HSTS) | Nothing to configure. |
| `X-Content-Type-Options: nosniff` | Automatic (Pages sends it) | Not settable via `<meta>`. |
| Content-Security-Policy | `<meta http-equiv>` in `index.html` / `404.html` | Self-only policy; all app assets are same-origin. |
| Referrer-Policy | `<meta name="referrer">` | `strict-origin-when-cross-origin`. |
| Clickjacking (`X-Frame-Options` / CSP `frame-ancestors`) | **Not available** | Requires a response header; `frame-ancestors` is ignored in a `<meta>` CSP. **Accepted gap on Pages.** If framing protection becomes a hard requirement, front Pages with a CDN that can inject headers (e.g. Cloudflare) or move to a header-capable host. |

The CSP intentionally keeps `script-src 'self'` (no `'unsafe-inline'`): the
service-worker registration was moved from an inline `<script>` into `app.js`
specifically so no inline script is required. `style-src` allows
`'unsafe-inline'` only because a handful of presentational `style=""` attributes
remain in the markup.

---

## Requirements for correct behavior

1. **Serve over HTTPS** (or `http://localhost`) so the service worker registers.
   Pages provides HTTPS automatically. The app still works without a service
   worker — offline caching is simply disabled.
2. **Serve `manifest.webmanifest` with a JSON-family content type.** GitHub
   Pages serves `.webmanifest` as `application/manifest+json` automatically.
3. **Service-worker freshness is driven by the cache key, not headers.** Pages
   applies its own short CDN caching to every file (including `sw.js`) and does
   **not** honor a `no-cache` directive — so freshness cannot rely on cache
   headers. Instead, whenever you change any precached asset (the `ASSETS`
   array in `sw.js`), **bump the `CACHE` constant** at the top of `sw.js`
   (currently `odisena-v4`). The `activate` handler deletes old caches, and the
   `sw-cache-bump` CI check fails the PR if a precached asset changes without a
   key bump. Returning clients pick up the new `sw.js` within the Pages CDN TTL.
4. No cookies, `localStorage`, `sessionStorage`, or `indexedDB` are used — all
   state is in-memory, so the app runs in restrictive/sandboxed contexts.

---

## Publishing a change (GitHub Pages)

Publishing is branch-based: **merge to `main` and Pages rebuilds the branch root
automatically.** There is nothing to run.

- **`.nojekyll`** must stay present so files (including any leading-underscore
  paths) are served verbatim. Do not remove it.
- The `ci.yml` checks are validation-only and the `avpt.yml` deploy step is an
  inert dry-run echo, so neither touches the published files.

### Custom domain (`console.odisena.com`)

The live console is served at **https://console.odisena.com/**. The committed
**`CNAME`** file contains exactly `console.odisena.com` (no scheme, no trailing
slash, single line) and **is** the GitHub Pages custom-domain binding — keep it
present and intact. If it is deleted, Pages falls back to the default
`*.github.io` URL and the custom domain has to be re-entered by hand in
**Settings → Pages**.

This retains an existing binding; it does **not** authorize any DNS change. The
`console.odisena.com` DNS records are managed outside this repository, and
committing/retaining the `CNAME` file touches no DNS provider. Do not modify DNS
or Pages settings as part of this change.

---

## Rollback

GitHub Pages has **no host-side deploy ledger to "promote a previous deploy."**
Because publishing tracks the `main` branch root, rollback is a git operation:

```bash
git revert <bad-commit-sha>   # creates an inverse commit; non-destructive
# open a PR, let CI pass, merge to main -> Pages republishes the prior state
```

Prefer `git revert` (which preserves history and passes the required checks)
over force-pushing `main`, which is blocked by branch protection anyway. The
linear-history + squash-only rule on `main` keeps one commit per change, so the
last known-good state is a single revertible ref.

---

## Verifying a deployment

- Open the site; confirm the home stats (Sessions / Runbooks / Artifacts) load.
- Open **Runbooks**, click any item — markdown should render.
- Open **Artifacts**, open one and use its explicit download action.
- DevTools → Application → Service Workers: `odisena-v4` should be active.
- DevTools → Application → Manifest: no errors, icons resolve.
- DevTools → Console: no Content-Security-Policy violations.
- Toggle airplane mode / offline and reload — the shell should still open.

---

## Updating content

Content lives in `catalog.json` plus the `runbooks/` and `artifacts/`
directories. To change what the app shows:

1. Add/replace files under `runbooks/` or `artifacts/`.
2. Update the matching entry in `catalog.json` (keep `path` relative).
3. Bump the `CACHE` constant in `sw.js` so clients refetch (CI enforces this).
4. Merge to `main`.

---

## Portability (other static hosts)

The app is host-agnostic and will run from any static host, object store, or
`nginx` root — copy the directory to the web root and serve `index.html` as the
index and `404.html` as the error document over HTTPS. If you move to a
header-capable host, that host can add the response headers Pages cannot
(`X-Frame-Options`, `Cache-Control`, etc.); those are deployment-environment
concerns and are intentionally not re-introduced as committed config files,
since production is GitHub Pages.
