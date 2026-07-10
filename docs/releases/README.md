# Release governance — RFPA / AVPT

Odisena Console promotes changes through three environments:

```
develop  ->  preview  ->  main
(integration)  (candidate)  (production: console.odisena.com)
```

Every promotion is governed by the **RFPA / AVPT** cadence. Branch protection
(see [`.github/rulesets`](../../.github/rulesets/README.md)) is the enforcement
surface; this directory holds the **written A/V/P/T logs** that the cadence
requires.

## RFPA — the framing

| Letter | Meaning    | In this repo |
| ------ | ---------- | ------------ |
| **R**  | Recover    | Every promotion must be able to recover the prior known-good state (reversible, non-destructive deploys; keep the previous static deploy re-promotable). |
| **F**  | Field      | Changes are exercised in a real environment (`develop` → `preview`) before production. |
| **P**  | Principal  | One accountable principal owns each promotion decision (solo operator today; scales via `CODEOWNERS`). |
| **A**  | Assumption | The assumptions behind a promotion are written down and re-checked each cycle. |

## AVPT — the cycle

One **AVPT cycle** per candidate change:

1. **Attempt** — exactly **one** deployment mutation (a single reversible change).
2. **Validate** — CI and/or environment evidence: the four required checks
   (`html-validate`, `catalog-schema`, `link-check`, `sw-cache-bump`) plus a
   manual smoke of the deployed PWA per `DEPLOYMENT.md` → *Verifying a deployment*.
3. **Preserve** — record the highest known-good deploy/rollback state (last-good
   commit SHA and/or deploy id) and keep it re-deployable.
4. **Track** — record the delta and the evidence before the next cycle starts.

## Gates (enforced by process + branch rulesets)

- **At least one AVPT cycle must be completed and logged before any merge/promotion.**
- **No cycle N+1 promotion until cycle N has a written A/V/P/T log** in this
  directory.
- **Preserve a reversible / non-destructive deployment posture at all times**
  (static host: keep the previous deploy instantly re-promotable / rollback on).
- **Promotion order is `develop → preview → main`.** A `main` promotion log must
  reference the `preview` AVPT log for the same candidate commit.

## How to log a cycle

1. Copy [`AVPT-TEMPLATE.md`](./AVPT-TEMPLATE.md) to
   `AVPT-<NNN>-<YYYYMMDD>.md` (e.g. `AVPT-001-20260710.md`).
2. Fill every section. Leave no `TBD` in **Validate**, **Preserve**, or the
   **Cycle-boundary sign-off** before promoting.
3. Commit the log in the same PR as the change it describes.

## Local validation (same checks CI runs)

```bash
python3 .github/scripts/validate_html.py
python3 .github/scripts/validate_catalog.py
python3 .github/scripts/check_links.py
python3 .github/scripts/check_sw_cache_bump.py --event pull_request \
  --base origin/<base-branch> --head HEAD
( cd .github/scripts && python3 test_sw_cache_bump.py )
```
