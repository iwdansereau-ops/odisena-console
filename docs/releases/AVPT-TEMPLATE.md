# AVPT Cycle <NNN> — <YYYY-MM-DD>

> Copy to `AVPT-<NNN>-<YYYYMMDD>.md`. One cycle = one Attempt (one deployment
> mutation). Fill every section; no `TBD` in Validate / Preserve / sign-off
> before promoting. See [README](./README.md).

## Header

| Field                     | Value |
| ------------------------- | ----- |
| Cycle number              | `<NNN>` |
| Date                      | `<YYYY-MM-DD>` |
| Principal (accountable)   | `@iwdansereau-ops` |
| Target environment        | `develop` \| `preview` \| `main` |
| Candidate commit (SHA)    | `<full-sha>` |
| PR                        | `#<n>` |
| Previous cycle log        | `AVPT-<NNN-1>-<...>.md` (or `n/a` for first cycle) |

---

## R/F/P/A — Assumptions & framing

- **Recover:** How the prior state is recoverable for this change:
  `<e.g. redeploy previous static build / instant rollback available>`
- **Field:** Environment(s) this was exercised in before this promotion:
  `<develop / preview>`
- **Principal:** `@iwdansereau-ops` owns this promotion decision.
- **Assumptions (explicit):**
  1. `<assumption>` — re-checked this cycle? `<yes/no + note>`
  2. `<assumption>`

---

## A — Attempt (single deployment mutation)

- **What changed (the one mutation):** `<concise description>`
- **Scope of changed files:** `<paths / summary>`
- **Service-worker-controlled assets touched?** `<yes/no>` — if yes, CACHE bumped
  from `<odisena-vX>` to `<odisena-vY>`.
- **Reversible?** `<yes — how / no — justification>`

---

## V — Validate (CI + environment evidence)

Required status checks (must be green on the candidate commit):

| Check            | Result | Notes |
| ---------------- | ------ | ----- |
| `html-validate`  | ⬜ pass/fail | |
| `catalog-schema` | ⬜ pass/fail | |
| `link-check`     | ⬜ pass/fail | |
| `sw-cache-bump`  | ⬜ pass/fail | |

- **CI run URL:** `<link>`
- **Environment / deploy evidence:** `<preview or prod deploy URL + result>`
- **Manual smoke** (per `DEPLOYMENT.md` → *Verifying a deployment*):
  - [ ] Home stats load (Sessions / Runbooks / Artifacts)
  - [ ] A runbook renders (markdown)
  - [ ] An artifact downloads
  - [ ] Service worker active (`odisena-v<...>`)
  - [ ] Manifest OK / icons resolve
  - [ ] Offline reload opens the shell

---

## P — Preserve (highest known-good state)

- **New known-good commit (SHA):** `<full-sha>`
- **Known-good deploy id / URL:** `<deploy id or URL>`
- **Previous known-good ref (rollback target):** `<sha / deploy id>`
- **Rollback procedure (verified re-deployable):**
  `<e.g. re-promote previous deploy in host UI / redeploy previous SHA>`
- **Rollback tested?** `<yes/no + note>`

---

## T — Track (delta + evidence for next cycle)

- **Delta since previous cycle:** `<commits / behavior change>`
- **Evidence archived:** `<links: CI, deploy, screenshots>`
- **Follow-ups / new assumptions to carry forward:** `<list or none>`

---

## Cycle-boundary sign-off

- [ ] Exactly one deployment mutation in this cycle (Attempt).
- [ ] All four required checks green on the candidate commit (Validate).
- [ ] Known-good state + rollback recorded and re-deployable (Preserve).
- [ ] Delta + evidence recorded (Track).
- [ ] Reversible / non-destructive posture preserved.
- [ ] (main only) References the `preview` AVPT log for this candidate commit:
  `<link>`

**Signed off by:** `@iwdansereau-ops`  **on** `<YYYY-MM-DD>`
**Cleared to promote to:** `<environment>`
