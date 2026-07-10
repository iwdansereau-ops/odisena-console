# Branch protection rulesets — Odisena Console

Source of truth for GitHub branch protection on this repository. The
machine-readable manifest is
[`odisena-branch-protection.rulesets.json`](./odisena-branch-protection.rulesets.json);
[`apply-rulesets.sh`](./apply-rulesets.sh) iterates it and applies each ruleset
via the GitHub REST API.

> One file, three targets. GitHub's API takes **one payload per ruleset**, so
> the JSON is a *manifest*: each entry in `rulesets[]` is a complete, API-valid
> payload. Keys prefixed with `_` are documentation/metadata and are stripped
> before any API call.

## Environment mapping

| Branch    | Environment                         | Ruleset name                   |
| --------- | ----------------------------------- | ------------------------------ |
| `develop` | integration / rapid feedback        | `odisena-develop-integration`  |
| `preview` | candidate validation / preview      | `odisena-preview-candidate`    |
| `main`    | production promotion (console.odisena.com) | `odisena-main-production` |

## Protection matrix

| Control                          | `main`            | `preview`         | `develop`         |
| -------------------------------- | ----------------- | ----------------- | ----------------- |
| Required approvals               | **2**             | 1                 | 0 (PR still req.) |
| Dismiss stale reviews on push    | yes               | yes               | no                |
| Require last-push approval       | yes               | yes               | no                |
| Conversation resolution required | yes               | yes               | no                |
| Require code-owner review        | yes¹              | no                | no                |
| Required signatures              | yes               | no                | no                |
| Linear history                   | yes               | yes               | no (merges OK)    |
| Block force-push (`non_fast_forward`) | yes          | yes               | yes               |
| Restrict deletion                | yes               | yes               | yes               |
| Allowed merge methods            | squash            | squash, merge     | squash, merge, rebase |
| Admin bypass                     | PR-merge only     | PR-merge only     | always            |
| Required status checks           | 4 (strict)²       | 4 (strict)²       | 2 (non-strict)²   |

¹ Enabled via `.github/CODEOWNERS` (`* @iwdansereau-ops`). The owner cannot
approve their own PR, so merges use the documented solo-admin `pull_request`
bypass until a second maintainer exists — see below.

## Status-check matrix (enforced)

CI is implemented in [`.github/workflows/ci.yml`](../workflows/ci.yml). Each
job's `name:` is the exact required-check context. Contexts are matched by name
(no `integration_id`), and the CI workflow is the only producer today.

| Context          | develop | preview | main | Script |
| ---------------- | :-----: | :-----: | :--: | ------ |
| `html-validate`  | ✅ | ✅ | ✅ | `.github/scripts/validate_html.py` |
| `catalog-schema` | ✅ | ✅ | ✅ | `.github/scripts/validate_catalog.py` |
| `link-check`     | –  | ✅ | ✅ | `.github/scripts/check_links.py` |
| `sw-cache-bump`  | –  | ✅ | ✅ | `.github/scripts/check_sw_cache_bump.py` |
| strict (up-to-date before merge) | no | **yes** | **yes** | — |

² `develop` deliberately uses a **non-strict** policy (rapid feedback: no forced
"branch up to date" requirement) and only the two fast structural checks.
`preview` and `main` are **strict** and require all four.

### Local reproduction

```bash
python3 .github/scripts/validate_html.py
python3 .github/scripts/validate_catalog.py
python3 .github/scripts/check_links.py
python3 .github/scripts/check_sw_cache_bump.py --event pull_request \
  --base origin/<base-branch> --head HEAD
( cd .github/scripts && python3 test_sw_cache_bump.py )   # sw-cache-bump unit tests
```

## Administrative / bypass policy

Solo-operator interim: an admin cannot approve their own PR, so a bypass is
required to merge at all with a single maintainer.

- `main` / `preview`: admin bypass in **`pull_request`** mode only — bypass
  happens through a PR merge, never via direct pushes. Force-push and deletion
  remain blocked for everyone.
- `develop`: admin bypass **`always`**, for fast integration.
- **`actor_id` must be verified before apply.** For `actor_type:
  RepositoryRole`, the id is the numeric role (conventionally `5`=Admin,
  `4`=Maintain, `2`=Write, `1`=Read). This is a user-owned repo, so there is no
  `OrganizationAdmin` actor.

When a second maintainer is onboarded: remove/tighten the bypass actors and keep
`main` at a hard 2-reviewer gate.

## Release governance — RFPA / AVPT

The ruleset is the enforcement surface for the release cadence encoded in the
manifest's `_manifest.release_governance`:

- **RFPA** = Recover, Field, Principal, Assumption.
- **AVPT** = Attempt, Validate, Preserve, Track — one deployment mutation per
  Attempt; Validate via CI/environment evidence; Preserve the highest known-good
  deploy/rollback state; Track the delta and evidence before the next cycle.
- **Gates:** at least one AVPT cycle must be completed and logged before any
  merge/promotion; no cycle N+1 promotion until cycle N has a written A/V/P/T
  log; keep a reversible / non-destructive deployment posture (static host: keep
  the previous deploy instantly re-promotable). Promotion order is
  `develop → preview → main`; a `main` promotion must reference the preview
  A/V/P/T log for the candidate commit.

The linear-history + squash-only rule on `main` keeps one commit per Attempt,
which makes the "Preserve highest known-good" state a single revertible ref.
AVPT logs live in [`docs/releases/`](../../docs/releases/README.md); each cycle
is recorded from [`AVPT-TEMPLATE.md`](../../docs/releases/AVPT-TEMPLATE.md). The
four required checks are the CI half of each cycle's **Validate** step.

## Applying (manual — do not automate blindly)

```bash
OWNER=iwdansereau-ops REPO=odisena-console ./apply-rulesets.sh
```

Requires `gh` (authenticated, `administration:write`) and `jq`. The script
creates each ruleset, or updates it in place if one with the same name already
exists.

## Implemented in this repo

- **CI status checks** — `.github/workflows/ci.yml` + `.github/scripts/*`
  (deterministic, offline, no secrets). Enforced per the matrix above.
- **CODEOWNERS** — `.github/CODEOWNERS`; `require_code_owner_review` on `main`.
- **AVPT logs** — `docs/releases/` (README + template).

## Remaining external-only gaps (still proposed, NOT enforced)

These depend on external platforms and cannot be implemented from the repo:

1. **External deploy checks not wired.** Vercel/Netlify commit-status checks
   (`Vercel`, `netlify/.../deploy-preview`) only appear once the repo is Git-
   connected in those platforms; enforce only after confirming the generated
   context names. Tracked in `_manifest._proposed_status_checks`.
2. **No GitHub deployment Environments.** Create `preview` and `production`
   Environments to enable a `required_deployments` gate on `preview`/`main`.
   Tracked in `_manifest._proposed_required_deployments`.
3. **Verify bypass `actor_id`** for the repo before apply (RepositoryRole id).
