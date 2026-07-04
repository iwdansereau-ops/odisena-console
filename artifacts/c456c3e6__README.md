# otelcol-custom-exporter

A minimal, runnable template for building a **custom OpenTelemetry Collector exporter** in Go, compiled into a distribution with the [OpenTelemetry Collector Builder (OCB)](https://opentelemetry.io/docs/collector/custom-collector/).

The example component, `loggingtrace`, receives OTLP traces from a Collector pipeline and logs them via the Collector's shared zap logger. It is deliberately simple so you can strip out the log-and-forget body and drop in your own backend call (HTTP, gRPC, Kafka, S3, etc.) without having to also learn the exporter, factory, and OCB plumbing.

## Layout

```
otelcol-custom-exporter/
├── README.md
├── exporter/
│   └── loggingtraceexporter/
│       ├── go.mod              # Module for the exporter component
│       ├── config.go           # Config struct + Validate()
│       ├── factory.go          # exporter.Factory with exporterhelper wiring
│       ├── exporter.go         # pushTraces implementation
│       └── component_shim.go   # Small import shim
└── build/
    ├── manifest.yaml           # OCB manifest that pulls in the component
    └── config.yaml             # Runtime Collector config using the exporter
```

Two important design choices to note:

- **The component lives in its own Go module** (`exporter/loggingtraceexporter/go.mod`). OCB expects each component listed under `gomod:` to be an importable module, and keeping it standalone makes it trivial to publish later at, e.g., `github.com/you/otelcol-loggingtrace`.
- **OCB owns the `main` package.** You do not check in a `main.go` — OCB generates one from `manifest.yaml` into `_build/` on every run.

## Prerequisites

- Go 1.23+
- [`ocb`](https://github.com/open-telemetry/opentelemetry-collector/releases) (`opentelemetry-collector-builder`) on your `$PATH`. Install with:

  ```bash
  go install go.opentelemetry.io/collector/cmd/builder@v0.116.0
  # the binary is installed as `builder`; alias or symlink to `ocb` if you prefer
  ln -sf "$(go env GOPATH)/bin/builder" "$(go env GOPATH)/bin/ocb"
  ```

- Optional, for end-to-end testing: [`telemetrygen`](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/cmd/telemetrygen):

  ```bash
  go install github.com/open-telemetry/opentelemetry-collector-contrib/cmd/telemetrygen@latest
  ```

## Build the custom Collector

From the repo root:

```bash
cd build
ocb --config manifest.yaml
```

OCB will:

1. Generate a `main` package under `_build/`.
2. Resolve every `gomod:` entry in `manifest.yaml`.
3. Follow the local `path:` for the `loggingtraceexporter` module (via a Go workspace replace directive).
4. Run `go mod tidy` and `go build`, producing `./_build/otelcol-custom`.

Verify the exporter was linked in:

```bash
./_build/otelcol-custom components | grep loggingtrace
```

You should see `loggingtrace` listed under `exporters`.

## Configure and run

`build/config.yaml` sets up a minimal traces pipeline: OTLP in → batch → `loggingtrace` + `debug`.

```bash
./_build/otelcol-custom --config ./config.yaml
```

On startup you should see:

```
loggingtrace exporter started  {"verbosity": "detailed", "prefix": "[loggingtrace]"}
Everything is ready. Begin running and processing data.
```

## End-to-end test

In a second shell, generate a handful of spans against the OTLP gRPC endpoint:

```bash
telemetrygen traces \
  --otlp-endpoint localhost:4317 \
  --otlp-insecure \
  --traces 3 \
  --child-spans 2
```

The Collector process should print lines that look like this:

```
[loggingtrace] received traces: resource_spans=1 spans=6
[loggingtrace] span   {"trace_id": "…", "span_id": "…", "name": "okey-dokey-0", ...}
```

If you want the batch summary only (much less noise at real traffic volumes), set `verbosity: basic` in `config.yaml` and restart the Collector.

## Extending the exporter

The typical customization is to replace the body of `pushTraces` in `exporter.go` with a call to your backend. A couple of tips:

- **Do not manage your own retry/queue.** `exporterhelper.WithRetry` and `WithQueue` already wrap `pushTraces` — return an error and let the helper handle backoff. Wrap transient errors with `consumererror.NewTraces(err, td)` if you need partial success semantics.
- **Respect `context.Done()`.** The context passed to `pushTraces` is cancelled on Collector shutdown; long-lived HTTP/gRPC calls should honor it.
- **Add metrics/logs support** by registering `exporter.WithMetrics` and `exporter.WithLogs` in `NewFactory`, each pointing at their own `pushMetrics` / `pushLogs` method with the same shape as `pushTraces`.

## Version pinning

`manifest.yaml`, the exporter's `go.mod`, and the `builder` install command all reference `v0.116.0` / `v1.22.0`. When you bump the Collector version, update all three together — mismatched versions across component modules and OCB are the single most common cause of `ocb` build failures.
