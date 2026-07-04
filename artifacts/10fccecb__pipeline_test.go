// Package otlpmetricstest is a runnable integration test for an OTLP/HTTP
// metrics pipeline. It wires up:
//
//  1. An httptest.Server acting as an OTLP/HTTP metrics collector receiver on
//     /v1/metrics. It decodes the incoming protobuf ExportMetricsServiceRequest
//     back into a pmetric.Metrics value.
//
//  2. The standard OTLP/HTTP metrics exporter (otlphttpexporter) built from
//     its public factory, pointed at the httptest.Server's URL with retry and
//     queueing disabled so the export is synchronous and deterministic.
//
//  3. A sample pmetric.Metrics message (a Sum metric with a resource attribute,
//     a scope, a data-point attribute set and a numeric value).
//
//  4. A verification step (TestTelemetry) that uses pmetrictest.CompareMetrics
//     to confirm the metric received by the fake collector matches the metric
//     that was sent — same protobuf structure, same attribute sets, over the
//     HTTP/1.1 transport that httptest.Server provides by default.
//
// Run with:
//
//	go test -run TestTelemetry -v ./...
package otlpmetricstest

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/open-telemetry/opentelemetry-collector-contrib/pkg/pdatatest/pmetrictest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/config/confighttp"
	"go.opentelemetry.io/collector/config/configretry"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
	"go.opentelemetry.io/collector/exporter/exportertest"
	"go.opentelemetry.io/collector/exporter/otlphttpexporter"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
)

// fakeOTLPCollector is a minimal OTLP/HTTP receiver backed by httptest.Server.
// It accepts POSTs to /v1/metrics with a protobuf-encoded
// ExportMetricsServiceRequest body, decodes it, and stores the resulting
// pmetric.Metrics for later inspection by the test.
type fakeOTLPCollector struct {
	srv *httptest.Server

	mu       sync.Mutex
	received []pmetric.Metrics
	proto    string // negotiated protocol reported by the request (HTTP/1.1)
	ua       string // User-Agent header seen on the last request
	ct       string // Content-Type header seen on the last request
}

func newFakeOTLPCollector(t *testing.T) *fakeOTLPCollector {
	t.Helper()
	f := &fakeOTLPCollector{}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/metrics", f.handleMetrics)
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeOTLPCollector) URL() string { return f.srv.URL }

func (f *fakeOTLPCollector) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "read body: "+err.Error(), http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	// Decode the OTLP protobuf ExportMetricsServiceRequest.
	req := pmetricotlp.NewExportRequest()
	if err := req.UnmarshalProto(body); err != nil {
		http.Error(w, "unmarshal proto: "+err.Error(), http.StatusBadRequest)
		return
	}

	f.mu.Lock()
	f.received = append(f.received, req.Metrics())
	f.proto = r.Proto
	f.ua = r.Header.Get("User-Agent")
	f.ct = r.Header.Get("Content-Type")
	f.mu.Unlock()

	// Respond with an empty successful ExportMetricsServiceResponse.
	resp := pmetricotlp.NewExportResponse()
	respBytes, err := resp.MarshalProto()
	if err != nil {
		http.Error(w, "marshal response: "+err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/x-protobuf")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(respBytes)
}

func (f *fakeOTLPCollector) Received() []pmetric.Metrics {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]pmetric.Metrics, len(f.received))
	copy(out, f.received)
	return out
}

// buildSampleMetrics returns a deterministic pmetric.Metrics value used both
// as the payload the exporter sends and as the expected value the test
// compares against.
func buildSampleMetrics(now time.Time) pmetric.Metrics {
	md := pmetric.NewMetrics()

	rm := md.ResourceMetrics().AppendEmpty()
	rattrs := rm.Resource().Attributes()
	rattrs.PutStr("service.name", "checkout")
	rattrs.PutStr("service.namespace", "shop")
	rattrs.PutStr("deployment.environment", "test")

	sm := rm.ScopeMetrics().AppendEmpty()
	sm.Scope().SetName("otlpmetricstest")
	sm.Scope().SetVersion("v0.1.0")

	m := sm.Metrics().AppendEmpty()
	m.SetName("orders.processed")
	m.SetDescription("Number of orders processed")
	m.SetUnit("{orders}")

	sum := m.SetEmptySum()
	sum.SetIsMonotonic(true)
	sum.SetAggregationTemporality(pmetric.AggregationTemporalityCumulative)

	dp := sum.DataPoints().AppendEmpty()
	dp.SetStartTimestamp(pcommon.NewTimestampFromTime(now.Add(-time.Minute)))
	dp.SetTimestamp(pcommon.NewTimestampFromTime(now))
	dp.SetIntValue(42)

	dpAttrs := dp.Attributes()
	dpAttrs.PutStr("http.method", "POST")
	dpAttrs.PutStr("http.route", "/checkout")
	dpAttrs.PutInt("http.status_code", 200)

	return md
}

// buildExporterConfig constructs a Config for the OTLP/HTTP exporter that
// targets the given endpoint. Retry and queueing are disabled so that a call
// to ConsumeMetrics performs exactly one HTTP request and returns any error
// from the request synchronously — which is what a hermetic integration test
// needs.
func buildExporterConfig(factory component.Factory, endpoint string) *otlphttpexporter.Config {
	cfg := factory.CreateDefaultConfig().(*otlphttpexporter.Config)
	cfg.ClientConfig = confighttp.ClientConfig{
		Endpoint: endpoint,
		Timeout:  5 * time.Second,
		// TLS is not required: httptest.NewServer is plain HTTP/1.1.
	}
	cfg.RetryConfig = configretry.BackOffConfig{Enabled: false}
	cfg.QueueConfig = exporterhelper.QueueBatchConfig{Enabled: false}
	cfg.Encoding = otlphttpexporter.EncodingProto
	return cfg
}

// TestTelemetry is the end-to-end verification. It sends a known metric
// through the OTLP/HTTP exporter to the fake collector, then decodes the
// received protobuf and confirms — via pmetrictest.CompareMetrics — that the
// received pmetric.Metrics is structurally identical to the sent one,
// including resource attributes, scope, metric definition, data-point
// attributes and value.
func TestTelemetry(t *testing.T) {
	// 1. Fake OTLP/HTTP receiver.
	collector := newFakeOTLPCollector(t)

	// 2. Build the exporter from its public factory and point it at the
	//    fake collector's /v1/metrics endpoint.
	factory := otlphttpexporter.NewFactory()
	cfg := buildExporterConfig(factory, collector.URL())

	settings := exportertest.NewNopSettings(factory.Type())
	settings.BuildInfo.Description = "otlpmetricstest"
	settings.BuildInfo.Version = "0.0.0-test"

	ctx := context.Background()
	exp, err := factory.CreateMetrics(ctx, settings, cfg)
	require.NoError(t, err, "create metrics exporter")

	require.NoError(t, exp.Start(ctx, componenttest.NewNopHost()), "start exporter")
	t.Cleanup(func() {
		require.NoError(t, exp.Shutdown(context.Background()), "shutdown exporter")
	})

	// 3. Build the sample metric and export it.
	now := time.Unix(1_700_000_000, 0).UTC()
	sent := buildSampleMetrics(now)

	// Give the exporter its own copy so the test can compare against `sent`
	// without worrying about aliasing.
	sendPayload := pmetric.NewMetrics()
	sent.CopyTo(sendPayload)

	require.NoError(t, exp.ConsumeMetrics(ctx, sendPayload), "consume metrics")

	// 4. Verify: exactly one request arrived, over HTTP/1.1, with the
	//    OTLP/HTTP protobuf content type, and the decoded pmetric.Metrics
	//    matches the sent one byte-for-byte at the pdata level.
	received := collector.Received()
	require.Len(t, received, 1, "expected exactly one export request")

	collector.mu.Lock()
	proto, ua, ct := collector.proto, collector.ua, collector.ct
	collector.mu.Unlock()

	assert.Equal(t, "HTTP/1.1", proto, "transport should be HTTP/1.1")
	assert.Equal(t, "application/x-protobuf", ct, "OTLP/HTTP protobuf content type")
	assert.Contains(t, ua, "otlpmetricstest/0.0.0-test", "exporter should send configured User-Agent")

	// Full pdata equivalence check.
	require.NoError(t,
		pmetrictest.CompareMetrics(sent, received[0]),
		"received metrics must match sent metrics",
	)

	// And explicit sanity checks on the fields we most care about, so a
	// failure gives a targeted diagnostic even if CompareMetrics changes.
	got := received[0]
	require.Equal(t, 1, got.ResourceMetrics().Len())
	gotRM := got.ResourceMetrics().At(0)

	svcName, ok := gotRM.Resource().Attributes().Get("service.name")
	require.True(t, ok, "service.name resource attribute present")
	assert.Equal(t, "checkout", svcName.Str())

	require.Equal(t, 1, gotRM.ScopeMetrics().Len())
	gotSM := gotRM.ScopeMetrics().At(0)
	assert.Equal(t, "otlpmetricstest", gotSM.Scope().Name())

	require.Equal(t, 1, gotSM.Metrics().Len())
	gotMetric := gotSM.Metrics().At(0)
	assert.Equal(t, "orders.processed", gotMetric.Name())
	assert.Equal(t, pmetric.MetricTypeSum, gotMetric.Type())

	gotSum := gotMetric.Sum()
	assert.True(t, gotSum.IsMonotonic())
	assert.Equal(t, pmetric.AggregationTemporalityCumulative, gotSum.AggregationTemporality())

	require.Equal(t, 1, gotSum.DataPoints().Len())
	gotDP := gotSum.DataPoints().At(0)
	assert.Equal(t, int64(42), gotDP.IntValue())

	method, ok := gotDP.Attributes().Get("http.method")
	require.True(t, ok)
	assert.Equal(t, "POST", method.Str())

	route, ok := gotDP.Attributes().Get("http.route")
	require.True(t, ok)
	assert.Equal(t, "/checkout", route.Str())

	code, ok := gotDP.Attributes().Get("http.status_code")
	require.True(t, ok)
	assert.Equal(t, int64(200), code.Int())
}
