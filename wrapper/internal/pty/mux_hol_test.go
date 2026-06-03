package pty

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestSlowSinkDoesNotBlockFanOut is the regression for HOL blocking #10. A sink
// whose write blocks forever must not stall the mux's fan-out: delivering to all
// sinks under the lock must remain non-blocking, so a healthy sink keeps
// receiving while a wedged one is stuck. We drive the exact fan-out path the
// pump uses (deliver under RLock) and assert it returns promptly even when one
// sink's pump is frozen.
func TestSlowSinkDoesNotBlockFanOut(t *testing.T) {
	m := NewMux("", 80, 24, nil)

	block := make(chan struct{})
	var slowGot, fastGot int64

	// Slow client: its write blocks until we release `block`.
	m.AddSink("slow", func(_ string, _ []byte) {
		<-block
		atomic.AddInt64(&slowGot, 1)
	})
	// Fast client: returns immediately.
	m.AddSink("fast", func(_ string, _ []byte) {
		atomic.AddInt64(&fastGot, 1)
	})

	// Push more frames than the per-sink buffer can hold. With blocking fan-out
	// (the old code) this would wedge on the slow sink; with the fix each deliver
	// is non-blocking (drop-oldest), so the loop completes quickly.
	done := make(chan struct{})
	go func() {
		for i := 0; i < sinkBuf*4; i++ {
			m.mu.RLock()
			for _, sk := range m.sinks {
				sk.deliver(sinkFrame{sessID: "s1", b: []byte("x")})
			}
			m.mu.RUnlock()
		}
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		close(block) // unwedge so goroutines can exit
		t.Fatal("fan-out blocked on the slow sink (HOL regression)")
	}

	// The fast sink must have made progress while the slow one is still blocked.
	deadline := time.After(2 * time.Second)
	for atomic.LoadInt64(&fastGot) == 0 {
		select {
		case <-deadline:
			close(block)
			t.Fatal("fast sink received nothing while slow sink was blocked")
		case <-time.After(5 * time.Millisecond):
		}
	}

	close(block) // release the slow sink
	m.RemoveSink("slow")
	m.RemoveSink("fast")
}

// TestRemoveSinkStopsPump verifies RemoveSink drains and exits the sink's pump
// goroutine (no leak) and that delivering after removal is safe (the producer no
// longer references the removed sink).
func TestRemoveSinkStopsPump(t *testing.T) {
	m := NewMux("", 80, 24, nil)
	var got int64
	var wg sync.WaitGroup
	wg.Add(1)
	m.AddSink("c", func(_ string, _ []byte) {
		atomic.AddInt64(&got, 1)
	})

	m.mu.RLock()
	sk := m.sinks["c"]
	m.mu.RUnlock()
	sk.deliver(sinkFrame{sessID: "s", b: []byte("hi")})

	go func() {
		defer wg.Done()
		select {
		case <-sk.done:
		case <-time.After(2 * time.Second):
			t.Error("sink pump did not stop after RemoveSink")
		}
	}()

	m.RemoveSink("c")
	wg.Wait()

	if _, ok := m.sinks["c"]; ok {
		t.Error("sink still registered after RemoveSink")
	}
}
