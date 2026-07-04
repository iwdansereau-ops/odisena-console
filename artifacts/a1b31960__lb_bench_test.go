// Package lbbench benchmarks two implementations of the loadbalancingexporter's
// per-trace-ID backend resolution:
//
//   1. rwmutexRouter — mirrors the current contrib/loadbalancingexporter design
//      (sync.RWMutex guarding hashRing + exporters map, RLock per lookup).
//   2. atomicRouter  — SOP §2.2 pattern (atomic.Pointer[snapshot] with an
//      immutable snapshot swapped on backend changes).
//
// The workload replicates the loadbalancer's ConsumeTraces hot path:
//   • N reader goroutines each generating fresh trace IDs and calling
//     exporterAndEndpoint(tid).
//   • An optional single writer goroutine invoking onBackendChanges() at a
//     fixed cadence to simulate DNS/K8s resolver churn.
//
// Both implementations do the same underlying work (FNV hash → ring lookup →
// map lookup) so any delta measures the primitive cost, not algorithm cost.
package lbbench

import (
	"crypto/rand"
	"fmt"
	"hash/fnv"
	"sort"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// -------- Shared types (identical to what both impls read) --------

// wrappedExporter is a stand-in for loadbalancingexporter's *wrappedExporter.
// Its content doesn't matter; only its address (pointer equality) is observed.
type wrappedExporter struct {
	endpoint string
	_pad     [56]byte // avoid false sharing across entries
}

// hashRing implements a consistent-hash lookup analogous to
// loadbalancingexporter/consistent_hashing.go. Small, allocation-free, and
// correct enough to make the benchmark honest: real ring lookup is O(log N)
// over sorted hashes with virtual nodes; we do the same.
type hashRing struct {
	positions []uint32          // sorted virtual-node hash positions
	owner     map[uint32]string // hash -> endpoint
}

const virtualNodesPerEndpoint = 100

func newHashRing(endpoints []string) *hashRing {
	r := &hashRing{owner: make(map[uint32]string, len(endpoints)*virtualNodesPerEndpoint)}
	for _, ep := range endpoints {
		for v := 0; v < virtualNodesPerEndpoint; v++ {
			h := fnv.New32a()
			fmt.Fprintf(h, "%s#%d", ep, v)
			pos := h.Sum32()
			r.positions = append(r.positions, pos)
			r.owner[pos] = ep
		}
	}
	sort.Slice(r.positions, func(i, j int) bool { return r.positions[i] < r.positions[j] })
	return r
}

func (r *hashRing) endpointFor(traceID []byte) string {
	h := fnv.New32a()
	h.Write(traceID)
	target := h.Sum32()
	// Binary search for first position >= target; wrap around.
	idx := sort.Search(len(r.positions), func(i int) bool { return r.positions[i] >= target })
	if idx == len(r.positions) {
		idx = 0
	}
	return r.owner[r.positions[idx]]
}

// -------- Impl 1: sync.RWMutex (current loadbalancingexporter design) --------

type rwmutexRouter struct {
	mu        sync.RWMutex
	ring      *hashRing
	exporters map[string]*wrappedExporter
}

func newRWMutexRouter(endpoints []string) *rwmutexRouter {
	exps := make(map[string]*wrappedExporter, len(endpoints))
	for _, ep := range endpoints {
		exps[ep] = &wrappedExporter{endpoint: ep}
	}
	return &rwmutexRouter{ring: newHashRing(endpoints), exporters: exps}
}

// exporterAndEndpoint mirrors loadbalancer.go's method exactly:
//   lb.updateLock.RLock(); defer lb.updateLock.RUnlock()
//   endpoint := lb.ring.endpointFor(identifier)
//   exp, found := lb.exporters[endpointWithPort(endpoint)]
func (r *rwmutexRouter) exporterAndEndpoint(traceID []byte) *wrappedExporter {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ep := r.ring.endpointFor(traceID)
	return r.exporters[ep]
}

// onBackendChanges rebuilds the ring and map, then swaps under the write lock.
func (r *rwmutexRouter) onBackendChanges(endpoints []string) {
	newRing := newHashRing(endpoints)
	newExps := make(map[string]*wrappedExporter, len(endpoints))
	for _, ep := range endpoints {
		newExps[ep] = &wrappedExporter{endpoint: ep}
	}
	r.mu.Lock()
	r.ring = newRing
	r.exporters = newExps
	r.mu.Unlock()
}

// -------- Impl 2: atomic.Pointer[snapshot] (SOP §2.2 pattern) --------

// snapshot is immutable after Store. Readers grab the pointer once per lookup
// and read both ring and map through it without any lock — a coherent view.
type snapshot struct {
	ring      *hashRing
	exporters map[string]*wrappedExporter
	version   uint64
}

type atomicRouter struct {
	snap    atomic.Pointer[snapshot]
	version atomic.Uint64
}

func newAtomicRouter(endpoints []string) *atomicRouter {
	r := &atomicRouter{}
	r.publish(endpoints)
	return r
}

func (r *atomicRouter) publish(endpoints []string) {
	exps := make(map[string]*wrappedExporter, len(endpoints))
	for _, ep := range endpoints {
		exps[ep] = &wrappedExporter{endpoint: ep}
	}
	s := &snapshot{
		ring:      newHashRing(endpoints),
		exporters: exps,
		version:   r.version.Add(1),
	}
	r.snap.Store(s) // release barrier — readers see a fully constructed snapshot
}

// exporterAndEndpoint is fully lock-free: one atomic load + two map/ring reads
// on a snapshot the reader uniquely holds (via the pointer) for this call.
func (r *atomicRouter) exporterAndEndpoint(traceID []byte) *wrappedExporter {
	s := r.snap.Load()
	ep := s.ring.endpointFor(traceID)
	return s.exporters[ep]
}

func (r *atomicRouter) onBackendChanges(endpoints []string) { r.publish(endpoints) }

// -------- Trace ID generator --------

// A per-goroutine PRNG so we don't serialize on crypto/rand's global reader.
type ridGen struct {
	state uint64
}

func newRidGen(seed uint64) *ridGen { return &ridGen{state: seed | 1} }

// xorshift64* — fast, non-cryptographic; sufficient for hash distribution.
func (g *ridGen) next(buf []byte) {
	x := g.state
	x ^= x >> 12
	x ^= x << 25
	x ^= x >> 27
	g.state = x
	v := x * 2685821657736338717
	for i := 0; i < 16 && i < len(buf); i++ {
		buf[i] = byte(v >> (i % 8 * 8))
		if i == 7 {
			// mix so upper 8 bytes are decorrelated
			v = v*6364136223846793005 + 1442695040888963407
		}
	}
}

// -------- Fixtures --------

var globalEndpoints = mkEndpoints(32) // realistic collector fan-out size

func mkEndpoints(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("backend-%02d.otel.svc.cluster.local:4317", i)
	}
	return out
}

var sink *wrappedExporter // prevents dead-code elimination

// -------- Benchmarks — read-only, no writer --------

// runReaders drives N reader goroutines against `get` for b.N total ops,
// evenly divided. Each goroutine has its own trace-ID generator so there
// is no false sharing on the RNG.
func runReaders(b *testing.B, workers int, get func([]byte) *wrappedExporter) {
	b.ReportAllocs()
	b.ResetTimer()

	var wg sync.WaitGroup
	perWorker := b.N / workers
	if perWorker == 0 {
		perWorker = 1
	}
	start := make(chan struct{})
	var localSink atomic.Pointer[wrappedExporter]

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(seed uint64) {
			defer wg.Done()
			gen := newRidGen(seed)
			buf := make([]byte, 16)
			<-start
			var last *wrappedExporter
			for i := 0; i < perWorker; i++ {
				gen.next(buf)
				last = get(buf)
			}
			localSink.Store(last)
		}(uint64(w)*0x9E3779B97F4A7C15 + 1)
	}
	close(start)
	wg.Wait()
	sink = localSink.Load()
}

func BenchmarkRWMutex_Workers1(b *testing.B)   { runReaders(b, 1, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkRWMutex_Workers4(b *testing.B)   { runReaders(b, 4, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkRWMutex_Workers8(b *testing.B)   { runReaders(b, 8, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkRWMutex_Workers16(b *testing.B)  { runReaders(b, 16, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkRWMutex_Workers32(b *testing.B)  { runReaders(b, 32, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkRWMutex_Workers64(b *testing.B)  { runReaders(b, 64, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkRWMutex_Workers128(b *testing.B) { runReaders(b, 128, newRWMutexRouter(globalEndpoints).exporterAndEndpoint) }

func BenchmarkAtomic_Workers1(b *testing.B)   { runReaders(b, 1, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkAtomic_Workers4(b *testing.B)   { runReaders(b, 4, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkAtomic_Workers8(b *testing.B)   { runReaders(b, 8, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkAtomic_Workers16(b *testing.B)  { runReaders(b, 16, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkAtomic_Workers32(b *testing.B)  { runReaders(b, 32, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkAtomic_Workers64(b *testing.B)  { runReaders(b, 64, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }
func BenchmarkAtomic_Workers128(b *testing.B) { runReaders(b, 128, newAtomicRouter(globalEndpoints).exporterAndEndpoint) }

// -------- Benchmarks — 64 readers + concurrent writer (backend churn) --------
//
// Simulates a DNS/K8s resolver churning the backend set every writeInterval.
// This is where atomic.Pointer's advantage is largest: writers do not block
// readers at all, while RWMutex writers force every reader to serialize.

const writeInterval = 500 * time.Microsecond

func runReadersWithWriter(b *testing.B, workers int, get func([]byte) *wrappedExporter, write func([]string)) {
	b.ReportAllocs()
	stop := make(chan struct{})
	var wwg sync.WaitGroup
	wwg.Add(1)
	go func() {
		defer wwg.Done()
		tick := time.NewTicker(writeInterval)
		defer tick.Stop()
		set := 0
		for {
			select {
			case <-stop:
				return
			case <-tick.C:
				// Alternate between two backend sets to force real rebuild work.
				if set%2 == 0 {
					write(globalEndpoints)
				} else {
					write(mkEndpoints(31)) // change size to bust caching
				}
				set++
			}
		}
	}()

	runReaders(b, workers, get)

	close(stop)
	wwg.Wait()
}

func BenchmarkRWMutex_Workers64_WithWriter(b *testing.B) {
	r := newRWMutexRouter(globalEndpoints)
	runReadersWithWriter(b, 64, r.exporterAndEndpoint, r.onBackendChanges)
}
func BenchmarkAtomic_Workers64_WithWriter(b *testing.B) {
	r := newAtomicRouter(globalEndpoints)
	runReadersWithWriter(b, 64, r.exporterAndEndpoint, r.onBackendChanges)
}

// -------- Correctness cross-check (not a benchmark) --------
//
// Ensures both implementations resolve the same trace ID to the same endpoint
// given the same backend set. If this ever fails, the benchmark comparison
// is meaningless because the two impls are doing different work.

func TestBothImplsAgree(t *testing.T) {
	rw := newRWMutexRouter(globalEndpoints)
	at := newAtomicRouter(globalEndpoints)
	gen := newRidGen(0xC0FFEE)
	buf := make([]byte, 16)
	for i := 0; i < 10000; i++ {
		gen.next(buf)
		a := rw.exporterAndEndpoint(buf).endpoint
		b := at.exporterAndEndpoint(buf).endpoint
		if a != b {
			t.Fatalf("disagreement on trace %x: rw=%s atomic=%s", buf, a, b)
		}
	}
}

// -------- Race-detector regression test --------
//
// go test -race exercises both implementations under concurrent readers +
// writer. Guarantees the benchmark itself is race-free before we trust its
// numbers.

func TestRaceRWMutex(t *testing.T) {
	r := newRWMutexRouter(globalEndpoints)
	stress(t, r.exporterAndEndpoint, r.onBackendChanges)
}
func TestRaceAtomic(t *testing.T) {
	r := newAtomicRouter(globalEndpoints)
	stress(t, r.exporterAndEndpoint, r.onBackendChanges)
}

func stress(t *testing.T, get func([]byte) *wrappedExporter, write func([]string)) {
	t.Helper()
	deadline := time.Now().Add(200 * time.Millisecond)
	var wg sync.WaitGroup
	for w := 0; w < 16; w++ {
		wg.Add(1)
		go func(seed uint64) {
			defer wg.Done()
			gen := newRidGen(seed)
			buf := make([]byte, 16)
			for time.Now().Before(deadline) {
				gen.next(buf)
				_ = get(buf)
			}
		}(uint64(w) + 1)
	}
	// Writer
	wg.Add(1)
	go func() {
		defer wg.Done()
		toggle := false
		for time.Now().Before(deadline) {
			if toggle {
				write(globalEndpoints)
			} else {
				write(mkEndpoints(31))
			}
			toggle = !toggle
			time.Sleep(500 * time.Microsecond)
		}
	}()
	wg.Wait()
	_ = cryptoRandKeep // referenced to keep the import used
}

// crypto/rand kept as a documented alternative seed source; not used in the
// hot path because it would serialize benchmark goroutines on a global mutex.
var cryptoRandKeep = func() int { b := make([]byte, 1); _, _ = rand.Read(b); return int(b[0]) }()
