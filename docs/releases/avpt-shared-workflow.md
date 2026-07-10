# Shared AVPT reusable workflow — integration & pin-upgrade guide

The Odisena Console consumes the org-shared AVPT reusable workflow from
[`iwdansereau-ops/avpt-cicd-dashboard`](https://github.com/iwdansereau-ops/avpt-cicd-dashboard).
It runs the four AVPT beats — **Attempt → Validate → Preserve → Track** — as a
single reusable workflow, so the cadence documented in
[`README.md`](./README.md) has a CI implementation and not just a written log.

The caller lives at
[`.github/workflows/avpt.yml`](../../.github/workflows/avpt.yml) and is pinned
to an immutable commit SHA:

```
uses: iwdansereau-ops/avpt-cicd-dashboard/.github/workflows/avpt-reusable.yml@4e033c739fe50c9cd470132c8005e23297877b19
```

## Posture in this repo (why it is safe)

This integration is deliberately **inert**. It observes and records; it does not
deploy.

| Concern | How it is handled |
| ------- | ----------------- |
| Trigger | `workflow_dispatch` only. It never runs on `push` / `pull_request`, so the existing [`ci.yml`](../../.github/workflows/ci.yml) required-check contexts (`html-validate`, `catalog-schema`, `link-check`, `sw-cache-bump`) and the branch rulesets are completely unaffected. |
| Deployment | `dry_run: true`. `deploy_command` / `rollback_command` are no-op `echo`s — no host is contacted, nothing is promoted or reverted. |
| Secrets | None. `receiver_url` is empty (telemetry + cycle-boundary gate disabled) and no `secrets:` are passed. |
| Validate beat | Reuses this repo's **actual** CI scripts (`validate_html.py`, `validate_catalog.py`, `check_links.py`, and the `test_sw_cache_bump.py` self-test). |
| Preserve beat | Reuses a **read-only** Vercel/config state snapshot — [`avpt_state_snapshot.py`](../../.github/scripts/avpt_state_snapshot.py) — which hashes the deploy-governing config (`vercel.json`, `netlify.toml`, `_headers`, `catalog.json`, `manifest.webmanifest`, `sw.js`, the app shell) plus the service-worker cache key. No mutation, no secrets, no network. |

Under `dry_run: true` the platform's `custom` snapshot adapter records the
snapshot command as metadata rather than executing infra mutations; the same
`avpt_state_snapshot.py` becomes a live (still read-only) snapshot if a future
real promotion sets `dry_run: false`.

## Running it

Actions → **avpt** → *Run workflow* → pick an `environment`
(`develop` → `preview` → `main`, matching the promotion order). All four beats
run in dry-run and upload the config-state snapshot as a build artifact.

## Upgrading the pin

The pin is an **immutable commit SHA on purpose** — a mutable branch/tag would
let upstream workflow changes execute here without review. To move to a newer
platform revision:

1. **Pick the target SHA** in `avpt-cicd-dashboard` (a specific commit on the
   default branch, not a branch name or tag).
2. **Review the diff** between the current pin
   (`4e033c739fe50c9cd470132c8005e23297877b19`) and the target, focusing on
   `.github/workflows/avpt-reusable.yml`:
   - the `workflow_call` **inputs/secrets contract** (renamed or newly-required
     inputs will break this caller);
   - the **injection-safety** posture (caller values must stay out of `${{ }}`
     inside `run:` blocks — enforced upstream by
     `scripts/validate_workflow_contract.py`);
   - any change to **dry-run defaults** for deploy / rollback / snapshot.
3. **Update the single `@<sha>`** in
   [`.github/workflows/avpt.yml`](../../.github/workflows/avpt.yml) and the pin
   reference in this document.
4. **Dry-run first.** Dispatch `avpt` with `environment: develop` and confirm
   Validate is green and Preserve uploads a snapshot artifact before relying on
   the new pin.
5. Keep `dry_run: true` and `receiver_url: ""` unless a real, secret-backed
   promotion is being introduced — that is a separate, reviewed change.

> Tip: pin by full 40-char SHA only. Never `@main` / `@vX`. If Dependabot or a
> similar bot proposes a bump, still perform step 2 by hand — the security value
> of pinning is the human review of the exact upstream revision you execute.
