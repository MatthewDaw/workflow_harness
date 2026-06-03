package transport

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// fakeWriter is a deterministic frameWriter standing in for the WS connection. It
// records every envelope written and can be told to fail after the Nth write
// (failAfter), simulating a connection that drops mid-flush.
type fakeWriter struct {
	seqs     []int64
	failAfter int // 0 = never fail
}

func (w *fakeWriter) WriteJSON(v interface{}) error {
	ef, ok := v.(eventFrame)
	if !ok {
		return nil
	}
	if w.failAfter > 0 && len(w.seqs) >= w.failAfter {
		return fmt.Errorf("connection dropped")
	}
	w.seqs = append(w.seqs, ef.Seq)
	return nil
}

// newBuf opens a fresh on-disk ring buffer in a temp dir.
func newBuf(t *testing.T) *RingBuffer {
	t.Helper()
	rb, err := OpenRingBuffer(filepath.Join(t.TempDir(), "buf.jsonl"))
	if err != nil {
		t.Fatalf("open buffer: %v", err)
	}
	return rb
}

// TestFlushAcksEachEnvelopeOnce is the deterministic regression for data-loss #9.
//
// The bug: replay() acked len(pending), then the live serve() loop drained the
// SAME envelopes still queued in sendCh and Ack(1)'d each over the now-compacted
// buffer, inflating 'acked' past the line count. A later batch then sat at buffer
// indices <= the inflated cursor and was silently skipped on the next replay —
// permanently lost.
//
// This test reproduces the corruption path directly: flush a batch (replay),
// simulate the stale sendCh signals firing again (each would re-flush), then
// append a NEW batch and flush once more. With the over-ack the new batch would
// be skipped (idx <= acked). With the fix — ack only what was actually drained
// from the buffer — every envelope is delivered exactly once.
func TestFlushAcksEachEnvelopeOnce(t *testing.T) {
	buf := newBuf(t)
	c := NewClient("ws://x", "tok", "inst", buf, nil)
	w := &fakeWriter{}

	// Batch A buffered offline (also queues stale sendCh signals via Send).
	const a = 5
	for i := int64(0); i < a; i++ {
		if err := c.Send(env(i)); err != nil {
			t.Fatalf("send %d: %v", i, err)
		}
	}

	// Replay drains + acks batch A.
	if err := c.replay(w); err != nil {
		t.Fatalf("replay A: %v", err)
	}

	// The live loop then fires for each of A's stale sendCh signals. Each calls
	// flush; the buffer is now empty so every one of these must be a no-op (this
	// is exactly where the old code over-acked Ack(1) per signal).
	for i := 0; i < a; i++ {
		if err := c.flush(w); err != nil {
			t.Fatalf("stale flush %d: %v", i, err)
		}
	}

	// Batch B appended after the over-ack window.
	const total = 10
	for i := int64(a); i < total; i++ {
		if err := c.Send(env(i)); err != nil {
			t.Fatalf("send %d: %v", i, err)
		}
	}
	if err := c.flush(w); err != nil {
		t.Fatalf("flush B: %v", err)
	}

	// Every seq 0..9 must have been written exactly once.
	counts := map[int64]int{}
	for _, s := range w.seqs {
		counts[s]++
	}
	for i := int64(0); i < total; i++ {
		if counts[i] == 0 {
			t.Fatalf("seq %d was never delivered (skipped) — over-ack regression; got %v", i, w.seqs)
		}
		if counts[i] > 1 {
			t.Fatalf("seq %d delivered %d times — duplicate; got %v", i, counts[i], w.seqs)
		}
	}

	// Buffer fully drained, cursor exact.
	pending, _ := buf.Pending()
	if len(pending) != 0 {
		t.Fatalf("buffer not drained: %d pending", len(pending))
	}
}

// TestFlushMidStreamFailureKeepsRemainderPending verifies that when the
// connection drops partway through a flush, only the envelopes actually written
// are acked — the rest stay pending and replay on the next connect (no loss, no
// over-ack).
func TestFlushMidStreamFailureKeepsRemainderPending(t *testing.T) {
	buf := newBuf(t)
	c := NewClient("ws://x", "tok", "inst", buf, nil)

	const total = 6
	for i := int64(0); i < total; i++ {
		if err := c.Send(env(i)); err != nil {
			t.Fatalf("send %d: %v", i, err)
		}
	}

	w := &fakeWriter{failAfter: 3} // drop after 3 writes
	if err := c.flush(w); err == nil {
		t.Fatal("expected flush to return the write error")
	}

	// 3 written, so 3 acked; 3 remain pending in order.
	pending, err := buf.Pending()
	if err != nil {
		t.Fatalf("pending: %v", err)
	}
	if len(pending) != 3 {
		t.Fatalf("want 3 pending after mid-stream drop, got %d (%+v)", len(pending), pending)
	}
	if pending[0].Seq != 3 {
		t.Fatalf("remaining pending should resume at seq 3, got %d", pending[0].Seq)
	}

	// Reconnect: a healthy writer delivers exactly the remaining 3.
	w2 := &fakeWriter{}
	if err := c.replay(w2); err != nil {
		t.Fatalf("replay remainder: %v", err)
	}
	if len(w2.seqs) != 3 || w2.seqs[0] != 3 || w2.seqs[2] != 5 {
		t.Fatalf("remainder replay = %v, want seqs 3,4,5", w2.seqs)
	}
	if after, _ := buf.Pending(); len(after) != 0 {
		t.Fatalf("buffer should be empty after full delivery, got %d", len(after))
	}
}

// --- integration smoke over a real in-process WebSocket ---

// fakeHQ is an in-process WebSocket endpoint standing in for Command HQ.
type fakeHQ struct {
	srv      *httptest.Server
	url      string
	mu       sync.Mutex
	gotSeqs  []int64
	received chan struct{}
}

func newFakeHQ(t *testing.T) *fakeHQ {
	t.Helper()
	h := &fakeHQ{received: make(chan struct{}, 1024)}
	up := websocket.Upgrader{}
	h.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		for {
			var msg struct {
				Action string `json:"action"`
				Seq    int64  `json:"seq"`
			}
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg.Action != "event" {
				continue
			}
			h.mu.Lock()
			h.gotSeqs = append(h.gotSeqs, msg.Seq)
			h.mu.Unlock()
			select {
			case h.received <- struct{}{}:
			default:
			}
		}
	}))
	h.url = "ws" + strings.TrimPrefix(h.srv.URL, "http")
	t.Cleanup(h.srv.Close)
	return h
}

func (h *fakeHQ) seqs() []int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]int64(nil), h.gotSeqs...)
}

func (h *fakeHQ) waitForDistinct(t *testing.T, want int, d time.Duration) {
	t.Helper()
	deadline := time.After(d)
	for {
		seen := map[int64]bool{}
		for _, s := range h.seqs() {
			seen[s] = true
		}
		if len(seen) >= want {
			return
		}
		select {
		case <-h.received:
		case <-deadline:
			t.Fatalf("timed out waiting for %d distinct seqs; got %v", want, h.seqs())
		}
	}
}

// TestClientDeliversOverRealSocket exercises the full Run → replay → serve path
// end to end against a live WS server, confirming buffered events are delivered.
func TestClientDeliversOverRealSocket(t *testing.T) {
	hq := newFakeHQ(t)
	buf := newBuf(t)
	c := NewClient(hq.url, "tok", "inst-0", buf, nil)

	const total = 8
	for i := int64(0); i < total; i++ {
		if err := c.Send(env(i)); err != nil {
			t.Fatalf("send %d: %v", i, err)
		}
	}
	go c.Run()
	defer c.Stop()

	hq.waitForDistinct(t, total, 5*time.Second)
	if after, _ := buf.Pending(); len(after) != 0 {
		t.Fatalf("buffer not drained after delivery: %d pending", len(after))
	}
}
