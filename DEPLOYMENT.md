# Deploying the Odisena Console

The Odisena Console is a **fully static** Progressive Web App. There is no
build step, no server-side code, and no external network dependency — every
asset (HTML, CSS, JS, the `marked` markdown parser, icons, runbooks, artifacts,
and `catalog.json`) is bundled in this directory. You can host it on any static
web host, an object store, or an internal file server.

It no longer relies on any Perplexity sandbox / preview (`/computer/a`) URLs:
all references are relative, so the site works identically from a domain root,
a sub-path, or a static object store.

---

## Directory layout

```
.
├── index.html              # App shell (hash-routed SPA)
├── app.js                  # Client logic (deferred)
├── styles.css              # Styles (light/dark via prefers-color-scheme)
├── marked.min.js           # Bundled minimal markdown parser (no CDN)
├── catalog.json            # Data index: sessions, runbooks, artifacts, stats
├── manifest.webmanifest    # PWA manifest
├── sw.js                   # Service worker (offline cache, same-origin only)
├── 404.html                # Static not-found fallback
├── robots.txt              # Crawler policy
├── icons/                  # PWA + favicon assets
├── runbooks/               # Markdown runbooks rendered in-app
├── artifacts/              # Downloadable artifacts (code, YAML, zips, HTML, PNG)
├── vercel.json             # Vercel headers/cache config
├── netlify.toml            # Netlify headers/cache config
└── _headers                # Cloudflare Pages / Netlify headers config
```

---

## Requirements for correct behavior

1. **Serve over HTTPS** (or `http://localhost`) so the service worker registers.
   The app still works without a service worker — offline caching is simply
   disabled.
2. **Serve `manifest.webmanifest` with a JSON-family content type.** Most hosts
   do this automatically; the included config files set
   `application/manifest+json` explicitly.
3. **Do not long-cache `sw.js`.** The provided host configs set it to
   `no-cache` so updates ship immediately. When you change any bundled asset,
   bump the `CACHE` constant at the top of `sw.js` (currently `odisena-v2`).
4. No cookies, `localStorage`, `sessionStorage`, or `indexedDB` are used —
   all state is in-memory, so the app runs in restrictive/sandboxed contexts.

---

## Option A — Vercel

The repo already contains `vercel.json` (cache + security headers).

```bash
npm i -g vercel          # if not installed
cd odisena-console-hosted
vercel                   # preview deploy
vercel --prod            # production deploy
```

No framework preset is needed — choose **"Other"** / static. Output directory
is the project root (`.`).

## Option B — Netlify

`netlify.toml` sets `publish = "."` with no build command.

```bash
npm i -g netlify-cli
cd odisena-console-hosted
netlify deploy           # draft
netlify deploy --prod    # production
```

Or drag-and-drop this folder into the Netlify UI ("Deploy manually").

## Option C — Cloudflare Pages

```bash
npm i -g wrangler
cd odisena-console-hosted
wrangler pages deploy . --project-name odisena-console
```

Build command: *(none)*. Output directory: `.`. The included `_headers` file
is applied automatically by Cloudflare Pages.

## Option D — AWS S3 + CloudFront (static)

```bash
# 1. Create/choose a bucket and upload
aws s3 sync . s3://YOUR_BUCKET --delete \
  --exclude "DEPLOYMENT.md" --exclude "README.md"

# 2. Set the correct content types / cache for the special files
aws s3 cp sw.js s3://YOUR_BUCKET/sw.js \
  --content-type "text/javascript" \
  --cache-control "no-cache, no-store, must-revalidate"

aws s3 cp manifest.webmanifest s3://YOUR_BUCKET/manifest.webmanifest \
  --content-type "application/manifest+json" \
  --cache-control "public, max-age=3600"
```

- Set the bucket's static-website **index document** to `index.html` and the
  **error document** to `404.html`.
- Front the bucket with CloudFront for HTTPS (required for the service worker).
- Recommended cache behavior: long cache for `icons/*`, `*.css`, `*.js`;
  short cache (or no-store) for `sw.js`, `index.html`, and `catalog.json`.

## Option E — Any generic static host / nginx

Copy the directory to your web root. Minimal nginx snippet:

```nginx
location = /sw.js {
    add_header Cache-Control "no-cache, no-store, must-revalidate";
    add_header Service-Worker-Allowed "/";
}
location = /manifest.webmanifest {
    types { application/manifest+json webmanifest; }
    default_type application/manifest+json;
}
location / {
    try_files $uri $uri/ /index.html;
}
```

---

## Recommended caching summary

| Path                    | Cache-Control                               |
| ----------------------- | ------------------------------------------- |
| `sw.js`                 | `no-cache, no-store, must-revalidate`       |
| `index.html`            | `public, max-age=0, must-revalidate`        |
| `catalog.json`          | `public, max-age=300`                       |
| `manifest.webmanifest`  | `public, max-age=3600`                      |
| `*.css`, `*.js`         | `public, max-age=604800`                    |
| `icons/*`               | `public, max-age=2592000, immutable`        |

---

## Updating content

Content lives in `catalog.json` plus the `runbooks/` and `artifacts/`
directories. To change what the app shows:

1. Add/replace files under `runbooks/` or `artifacts/`.
2. Update the matching entry in `catalog.json` (keep `path` relative).
3. Bump the `CACHE` constant in `sw.js` so clients refetch.
4. Re-deploy.

---

## Verifying a deployment

- Open the site; confirm the home stats (Sessions / Runbooks / Artifacts) load.
- Open **Runbooks**, click any item — markdown should render.
- Open **Artifacts**, confirm files download.
- Check DevTools → Application → Service Workers: `odisena-v2` should be active.
- Check DevTools → Application → Manifest: no errors, icons resolve.
- Toggle airplane mode / offline and reload — the shell should still open.
