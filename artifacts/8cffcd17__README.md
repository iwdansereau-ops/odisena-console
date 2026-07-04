# otelcol-logexporter

A minimal, runnable template for building a **custom OpenTelemetry Collector
exporter** in Go and assembling it into a Collector binary using the
[OpenTelemetry Collector Builder (OCB)](https://opentelemetry.io/docs/collector/custom-collector/).

The exporter itself — called `log` — is deliberately simple: for every batch of
OTLP traces it receives, it emits structured log lines through the Collector's
own logger. That makes it a great scaffold for building your own real backend
integration: swap `pushTraces` for whatever your destination needs (HTTP, gRPC,
Kafka, a SaaS SDK, …) and keep everything else.

## Repository layout

```
otelcol-logexporter/
├── exporter/
│   └── logexporter/          # The custom component (its own Go module)
│       ├── go.mod
│       ├── config.go         # Config struct + Validate()
│       ├── factory.go        # NewFactory() + exporterhelper wiring
│       ├── exporter.go       # pushTraces implementation (logs each span)
│       └── factory_test.go   # Smoke tests
├── builder/
│   └── manifest.yaml         # OCB build manifest
├── config.yaml               # Runtime Collector config that uses `log:`
└── README.md
```

The `exporter/logexporter` directory is a **separate Go module** on purpose:
OCB expects each component to be independently `go get`-able, and keeping it
isolated means you can publish just the exporter without dragging in the
Collector distribution's build files.

## Prerequisites

- Go 1.23 or newer.
- `ocb` (the OpenTelemetry Collector Builder). Install the version that matches
  `otelcol_version` in `builder/manifest.yaml`:

  ```bash
  go install go.opentelemetry.io/collector/cmd/builder@v0.155.0
  # The binary is installed as `builder`; the docs call it `ocb`. Either works.
  # Optionally alias it: alias ocb=builder
  ```

  Or grab a prebuilt release from the
  [collector-releases](https://github.com/open-telemetry/opentelemetry-collector-releases/releases)
  page.

## Build the custom Collector

From the repository root:

```bash
# 1. Tidy the exporter module so its go.sum is up to date.
(cd exporter/logexporter && go mod tidy)

# 2. Run OCB against the manifest. This generates ./dist/ containing a
#    fully-formed Collector program that imports your exporter, then compiles
#    it into ./dist/otelcol-custom.
ocb --config builder/manifest.yaml
```

The important bits of `builder/manifest.yaml`:

```yaml
dist:
  name: otelcol-custom
  output_path: ./dist
  otelcol_version: 0.155.0

exporters:
  - gomod: github.com/example/otelcol-logexporter/exporter/logexporter v0.1.0
    path: ../exporter/logexporter        # <- local path replaces the module
  - gomod: go.opentelemetry.io/collector/exporter/debugexporter v0.155.0
```

The `path:` field on the first exporter is what lets OCB pick up your local,
unpublished code. Remove it once you publish the module to a real import path.

## Run it end-to-end

1. **Start the Collector** using the provided `config.yaml`:

   ```bash
   ./dist/otelcol-custom --config config.yaml
   ```

   You should see startup lines including:

   ```
   info    logexporter    log exporter started    {"verbosity": "normal"}
   info    service ...    Everything is ready. Begin running and processing data.
   ```

2. **Send some test traces.** The easiest way is `telemetrygen`:

   ```bash
   go install github.com/open-telemetry/opentelemetry-collector-contrib/cmd/telemetrygen@latest
   telemetrygen traces --otlp-insecure --duration 5s
   ```

   You can also point any OpenTelemetry-instrumented app at `localhost:4317`
   (gRPC) or `localhost:4318` (HTTP).

3. **Watch the exporter log spans.** In the Collector's stdout you'll see one
   line per span with fields like `trace_id`, `span_id`, `name`, `kind`, and
   `duration`. At `verbosity: detailed`, resource + span attributes are
   included too. `verbosity: basic` collapses each batch into a single line —
   useful for high-volume smoke tests.

## Configuration reference

```yaml
exporters:
  log:
    # basic   -> one line per batch
    # normal  -> one line per span with core fields (default)
    # detailed-> one line per span with resource + span attributes
    verbosity: normal

    # Standard exporterhelper knobs; you get these for free.
    timeout: 5s
    sending_queue:
      enabled: true
      num_consumers: 2
      queue_size: 1000
    retry_on_failure:
      enabled: true
      initial_interval: 1s
      max_interval: 10s
      max_elapsed_time: 60s
```

## How the pieces fit together

- `factory.go` calls `exporter.NewFactory(...)` with a
  `component.MustNewType("log")` identifier — that's the string users type
  under `exporters:` in `config.yaml`.
- `createTracesExporter` wraps the concrete implementation with
  `exporterhelper.NewTraces`, which layers timeout, retry, and queue behavior
  on top of your `pushTraces` function. You don't have to implement those
  yourself.
- `exporter.go` holds `pushTraces`, the hot path. It walks
  `ResourceSpans -> ScopeSpans -> Spans` and emits one zap log line per span.
- OCB reads `builder/manifest.yaml`, generates a `main.go` that imports
  `logexporter.NewFactory`, and compiles everything into a single binary.

## Extending to metrics and logs

Add two more create functions in `factory.go`, register them with
`exporter.WithMetrics` and `exporter.WithLogs`, and implement `pushMetrics` /
`pushLogs` in `exporter.go` — the shape mirrors `pushTraces` but iterates over
`pmetric.Metrics` and `plog.Logs` respectively.

## Running the tests

```bash
cd exporter/logexporter
go test ./...
```

## Troubleshooting

- **`ocb: cannot find module`** — make sure the `path:` field in
  `manifest.yaml` is correct relative to the location of `manifest.yaml`
  itself, not the working directory.
- **`unknown exporter type: log`** — you're running an official Collector
  distribution instead of `./dist/otelcol-custom`. Only the OCB-built binary
  has the custom exporter compiled in.
- **Version mismatch errors during `ocb`** — pin every
  `go.opentelemetry.io/collector/...` line in `manifest.yaml` to the same
  `otelcol_version`, and use the matching `v1.x.y` line for `component`,
  `pdata`, and `confmap` modules (they release on a separate cadence).
