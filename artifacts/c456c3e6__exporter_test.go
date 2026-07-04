package loggingtraceexporter

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/config/configretry"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/consumer/consumererror"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
	"go.opentelemetry.io/collector/exporter/exportertest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

// ---------- helpers ----------

// newObservedSettings returns exporter.Settings whose logger writes into an
// in-memory observer.ObservedLogs sink. Tests can then assert on structured
// fields exactly as they'd appear in production logs, without capturing stdout.
func newObservedSettings(tb testing.TB) (exporter.Settings, *observer.ObservedLogs) {
	tb.Helper()
	core, logs := observer.New(zapcore.DebugLevel)
	set := exportertest.NewNopSettings(componentType)
	set.Logger = zap.New(core)
	return set, logs
}

// buildTraces constructs a deterministic ptrace.Traces payload with the
// requested shape. Every span gets predictable IDs derived from its index
// so tests can look them up by string form.
func buildTraces(t *testing.T, resources, scopesPerResource, spansPerScope int) ptrace.Traces {
	t.Helper()
	td := ptrace.NewTraces()
	spanCounter := 0
	for r := 0; r < resources; r++ {
		rs := td.ResourceSpans().AppendEmpty()
		rs.Resource().Attributes().PutStr("service.name", fmt.Sprintf("svc-%d", r))
		rs.Resource().Attributes().PutInt("resource.index", int64(r))

		for s := 0; s < scopesPerResource; s++ {
			ss := rs.ScopeSpans().AppendEmpty()
			ss.Scope().SetName(fmt.Sprintf("scope-%d-%d", r, s))
			ss.Scope().SetVersion("v1.0.0")

			for k := 0; k < spansPerScope; k++ {
				sp := ss.Spans().AppendEmpty()
				sp.SetName(fmt.Sprintf("span-%d-%d-%d", r, s, k))
				sp.SetKind(ptrace.SpanKindServer)
				sp.SetStartTimestamp(pcommon.NewTimestampFromTime(time.Unix(0, 0)))
				sp.SetEndTimestamp(pcommon.NewTimestampFromTime(time.Unix(1, 0)))
				sp.Attributes().PutStr("http.method", "GET")

				// Encode counter into deterministic IDs.
				var tid [16]byte
				var sid [8]byte
				tid[15] = byte(spanCounter + 1)
				sid[7] = byte(spanCounter + 1)
				sp.SetTraceID(tid)
				sp.SetSpanID(sid)
				spanCounter++
			}
		}
	}
	return td
}

// ---------- config & factory ----------

func TestConfig_Validate(t *testing.T) {
	cases := []struct {
		name    string
		cfg     Config
		wantErr bool
	}{
		{"default empty is ok", Config{Verbosity: ""}, false},
		{"basic ok", Config{Verbosity: "basic"}, false},
		{"detailed ok", Config{Verbosity: "detailed"}, false},
		{"invalid rejected", Config{Verbosity: "loud"}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.cfg.Validate()
			if tc.wantErr {
				assert.Error(t, err)
			} else {
				assert.NoError(t, err)
			}
		})
	}
}

func TestFactory_CreatesTracesExporter(t *testing.T) {
	f := NewFactory()
	assert.Equal(t, componentType, f.Type())

	cfg := f.CreateDefaultConfig().(*Config)
	assert.Equal(t, "basic", cfg.Verbosity)
	assert.Equal(t, "[loggingtrace]", cfg.Prefix)
	require.NoError(t, cfg.Validate())

	set, _ := newObservedSettings(t)
	exp, err := f.CreateTraces(context.Background(), set, cfg)
	require.NoError(t, err)
	require.NotNil(t, exp)

	// Capabilities must be non-mutating so the pipeline can fan out safely.
	assert.False(t, exp.Capabilities().MutatesData)

	require.NoError(t, exp.Start(context.Background(), componenttest.NewNopHost()))
	require.NoError(t, exp.Shutdown(context.Background()))
}

// ---------- span iteration & logger invocation ----------

func TestPushTraces_BasicVerbosityLogsSummaryOnly(t *testing.T) {
	set, logs := newObservedSettings(t)
	e, err := newTracesExporter(&Config{Verbosity: "basic", Prefix: "[test]"}, set)
	require.NoError(t, err)

	td := buildTraces(t, 2, 2, 3) // 2 * 2 * 3 = 12 spans
	require.NoError(t, e.pushTraces(context.Background(), td))

	// Exactly one log line: the batch summary. No per-span lines in basic mode.
	all := logs.All()
	require.Len(t, all, 1, "basic verbosity should emit exactly one summary line")

	msg := all[0].Message
	assert.Contains(t, msg, "[test] received traces:")
	assert.Contains(t, msg, "resource_spans=2")
	assert.Contains(t, msg, "spans=12")
}

func TestPushTraces_DetailedVerbosityLogsEverySpan(t *testing.T) {
	set, logs := newObservedSettings(t)
	e, err := newTracesExporter(&Config{Verbosity: "detailed", Prefix: "[test]"}, set)
	require.NoError(t, err)

	td := buildTraces(t, 2, 2, 3) // 12 spans total
	require.NoError(t, e.pushTraces(context.Background(), td))

	all := logs.All()
	// 1 summary line + 12 per-span lines.
	require.Len(t, all, 13, "detailed verbosity should emit 1 summary + 1 line per span")

	spanEntries := logs.FilterMessage("[test] span").All()
	require.Len(t, spanEntries, 12)

	// Verify every span from the input appears in the log output. This is
	// the real test of iteration correctness: resource/scope/span nesting.
	seenTraceIDs := map[string]bool{}
	seenNames := map[string]bool{}
	for _, entry := range spanEntries {
		fields := entry.ContextMap()

		// Required structured fields — asserts contract with downstream
		// log consumers.
		for _, key := range []string{
			"trace_id", "span_id", "parent_span_id", "name", "kind",
			"status", "start", "end", "scope", "resource_attrs", "span_attrs",
		} {
			assert.Contains(t, fields, key, "span log missing field %q", key)
		}

		seenTraceIDs[fields["trace_id"].(string)] = true
		seenNames[fields["name"].(string)] = true

		// Scope should be formatted "name@version".
		assert.Contains(t, fields["scope"].(string), "@v1.0.0")
		// Resource attrs should carry the service.name we set.
		assert.Contains(t, fields["resource_attrs"].(string), "service.name=svc-")
		// Span attrs should carry the http.method we set.
		assert.Equal(t, "http.method=GET", fields["span_attrs"].(string))
	}

	assert.Len(t, seenTraceIDs, 12, "each span must have a unique trace_id in logs")
	assert.Len(t, seenNames, 12, "each span name must appear exactly once")
}

func TestPushTraces_EmptyTraces(t *testing.T) {
	set, logs := newObservedSettings(t)
	e, err := newTracesExporter(&Config{Verbosity: "detailed", Prefix: "[test]"}, set)
	require.NoError(t, err)

	require.NoError(t, e.pushTraces(context.Background(), ptrace.NewTraces()))

	all := logs.All()
	require.Len(t, all, 1)
	assert.Contains(t, all[0].Message, "resource_spans=0 spans=0")
}

func TestAttrsToString(t *testing.T) {
	m := pcommon.NewMap()
	assert.Empty(t, attrsToString(m), "empty map should render as empty string")

	m.PutStr("a", "1")
	m.PutInt("b", 2)
	out := attrsToString(m)
	// pcommon.Map.Range order is not guaranteed; check both keys present.
	assert.Contains(t, out, "a=1")
	assert.Contains(t, out, "b=2")
}

// ---------- start / shutdown lifecycle ----------

func TestLifecycle_StartAndShutdownLog(t *testing.T) {
	set, logs := newObservedSettings(t)
	e, err := newTracesExporter(&Config{Verbosity: "detailed", Prefix: "[lc]"}, set)
	require.NoError(t, err)

	require.NoError(t, e.start(context.Background(), componenttest.NewNopHost()))
	require.NoError(t, e.shutdown(context.Background()))

	require.Len(t, logs.FilterMessage("loggingtrace exporter started").All(), 1)
	require.Len(t, logs.FilterMessage("loggingtrace exporter shutting down").All(), 1)
}

func TestCapabilities(t *testing.T) {
	e := &tracesExporter{}
	assert.Equal(t, consumer.Capabilities{MutatesData: false}, e.Capabilities())
}

// ---------- retry mechanism ----------

// TestRetry_TransientFailureTriggersRetries wires the exporter through
// exporterhelper end-to-end and simulates a transient backend failure. It
// asserts that:
//  1. exporterhelper actually re-invokes pushTraces after a failure,
//  2. once pushOverride starts returning nil, the send succeeds,
//  3. our per-span logging ran on every attempt (so nothing is swallowed).
//
// The retry helper uses configretry.BackOffConfig for its schedule; we
// compress the intervals to keep the test fast but leave the state machine
// otherwise untouched.
func TestRetry_TransientFailureTriggersRetries(t *testing.T) {
	set, logs := newObservedSettings(t)

	cfg := &Config{
		Verbosity:       "basic",
		Prefix:          "[retry]",
		TimeoutSettings: exporterhelper.NewDefaultTimeoutConfig(),
		QueueSettings:   exporterhelper.NewDefaultQueueConfig(),
		BackOffConfig: configretry.BackOffConfig{
			Enabled:         true,
			InitialInterval: 5 * time.Millisecond,
			MaxInterval:     20 * time.Millisecond,
			MaxElapsedTime:  2 * time.Second,
			Multiplier:      1.5,
			RandomizationFactor: 0,
		},
	}
	// Disable the queue so pushes happen on the caller goroutine and we can
	// observe retry ordering deterministically.
	cfg.QueueSettings.Enabled = false

	inner, err := newTracesExporter(cfg, set)
	require.NoError(t, err)

	var attempts atomic.Int32
	const failuresBeforeSuccess = 3

	inner.pushOverride = func(_ context.Context, _ ptrace.Traces) error {
		n := attempts.Add(1)
		if n <= failuresBeforeSuccess {
			// exporterhelper treats plain errors as retryable by default.
			return fmt.Errorf("simulated transient failure #%d", n)
		}
		return nil
	}

	exp, err := exporterhelper.NewTraces(
		context.Background(),
		set,
		cfg,
		inner.pushTraces,
		exporterhelper.WithCapabilities(inner.Capabilities()),
		exporterhelper.WithStart(inner.start),
		exporterhelper.WithShutdown(inner.shutdown),
		exporterhelper.WithTimeout(cfg.TimeoutSettings),
		exporterhelper.WithRetry(cfg.BackOffConfig),
		exporterhelper.WithQueue(cfg.QueueSettings),
	)
	require.NoError(t, err)

	require.NoError(t, exp.Start(context.Background(), componenttest.NewNopHost()))
	t.Cleanup(func() { _ = exp.Shutdown(context.Background()) })

	td := buildTraces(t, 1, 1, 2)
	err = exp.ConsumeTraces(context.Background(), td)
	require.NoError(t, err, "consume should succeed once transient failures clear")

	got := attempts.Load()
	require.Equal(t, int32(failuresBeforeSuccess+1), got,
		"expected %d retries + 1 success, got %d", failuresBeforeSuccess, got)

	// pushTraces logs its summary line every invocation — retries included.
	// This proves the retry loop actually re-enters our function body rather
	// than short-circuiting somewhere upstream.
	summaries := logs.FilterMessageSnippet("received traces:").All()
	require.Len(t, summaries, int(got),
		"summary log should appear once per push attempt (including retries)")
}

// TestRetry_PermanentErrorIsNotRetried verifies the classification contract:
// errors wrapped with consumererror.NewPermanent must NOT trigger retries.
// This matters because misclassifying a permanent 4xx as retryable is one
// of the most common ways custom exporters silently melt Collector queues.
func TestRetry_PermanentErrorIsNotRetried(t *testing.T) {
	set, _ := newObservedSettings(t)

	cfg := &Config{
		Verbosity:       "basic",
		Prefix:          "[perm]",
		TimeoutSettings: exporterhelper.NewDefaultTimeoutConfig(),
		QueueSettings:   exporterhelper.NewDefaultQueueConfig(),
		BackOffConfig: configretry.BackOffConfig{
			Enabled:         true,
			InitialInterval: 5 * time.Millisecond,
			MaxInterval:     20 * time.Millisecond,
			MaxElapsedTime:  2 * time.Second,
			Multiplier:      1.5,
		},
	}
	cfg.QueueSettings.Enabled = false

	inner, err := newTracesExporter(cfg, set)
	require.NoError(t, err)

	var attempts atomic.Int32
	permErr := errors.New("bad request: schema mismatch")
	inner.pushOverride = func(_ context.Context, _ ptrace.Traces) error {
		attempts.Add(1)
		return consumererror.NewPermanent(permErr)
	}

	exp, err := exporterhelper.NewTraces(
		context.Background(),
		set,
		cfg,
		inner.pushTraces,
		exporterhelper.WithTimeout(cfg.TimeoutSettings),
		exporterhelper.WithRetry(cfg.BackOffConfig),
		exporterhelper.WithQueue(cfg.QueueSettings),
	)
	require.NoError(t, err)
	require.NoError(t, exp.Start(context.Background(), componenttest.NewNopHost()))
	t.Cleanup(func() { _ = exp.Shutdown(context.Background()) })

	err = exp.ConsumeTraces(context.Background(), buildTraces(t, 1, 1, 1))
	require.Error(t, err)
	assert.ErrorIs(t, err, permErr)
	assert.Equal(t, int32(1), attempts.Load(), "permanent errors must not be retried")
}

// Compile-time sanity: NewFactory must return an exporter.Factory. Catches
// regressions where the return type gets accidentally narrowed.
var _ exporter.Factory = NewFactory()

// Unused-but-imported guard for component so `goimports` doesn't strip it
// on machines running an aggressive linter. component is used transitively
// via componenttest.NewNopHost.
var _ = component.StabilityLevelAlpha
