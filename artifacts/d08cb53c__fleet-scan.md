# Fleet memory-check readiness — user/iwdansereau-ops

_Scanned: 2026-07-02T05:38:56Z_

**Totals:** 2 repos · ✅ ready: 1 · 🟡 needs handler: 0 · ⚪ not Go: 1 · ⚠️ clone failed: 0

## ✅ Ready — turn on the gate

These repos already expose `/debug/memstats` and call `runtime.ReadMemStats`. Drop in the 6-line caller and configure branch protection.

- [ ] **iwdansereau-ops/gomem-dashboard** — branch `main`, workflow present: true
      Handler in: cmd/gomem/main.go, cmd/sample-processor/main.go, internal/gcstats/gcstats.go

## 🟡 Needs handler — add /debug/memstats

Go repos that don't yet expose the endpoint. Add the 4-line handler (see repo README) and secrets, then enable the workflow.


## ⚪ Not Go — nothing to do

- iwdansereau-ops/otel-collector-config

