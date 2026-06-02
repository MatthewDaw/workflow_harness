// Package transport is the wrapper's outbound link to Command HQ: a WebSocket
// client (KTD6) that streams event envelopes with an on-disk ring buffer for
// offline durability and ordered replay on reconnect (U14), plus the control
// receiver that applies inject/pause/interrupt to a target session PTY (U15).
package transport

import (
	"bufio"
	"encoding/json"
	"os"
	"sync"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// RingBuffer is an append-only, on-disk buffer of envelopes pending acknowledged
// delivery. It survives daemon restarts so no event is lost while HQ is
// unreachable. Envelopes are stored newline-delimited JSON; a separate cursor
// file records how far HQ has acknowledged so replay resumes precisely (no gaps,
// no dupes — monotonic seq is preserved by the producer).
type RingBuffer struct {
	mu     sync.Mutex
	path   string
	cursor string // cursor file path (acked count)
	acked  int64
}

// OpenRingBuffer opens (or creates) a ring buffer at path.
func OpenRingBuffer(path string) (*RingBuffer, error) {
	rb := &RingBuffer{path: path, cursor: path + ".cursor"}
	if b, err := os.ReadFile(rb.cursor); err == nil {
		_ = json.Unmarshal(b, &rb.acked)
	}
	// Ensure the buffer file exists.
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	_ = f.Close()
	return rb, nil
}

// Append durably stores an envelope for delivery.
func (rb *RingBuffer) Append(env event.Envelope) error {
	rb.mu.Lock()
	defer rb.mu.Unlock()
	f, err := os.OpenFile(rb.path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	b, err := env.Marshal()
	if err != nil {
		return err
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		return err
	}
	return f.Sync()
}

// Pending returns all envelopes appended but not yet acknowledged, in order.
func (rb *RingBuffer) Pending() ([]event.Envelope, error) {
	rb.mu.Lock()
	defer rb.mu.Unlock()
	f, err := os.Open(rb.path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []event.Envelope
	var idx int64
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		idx++
		if idx <= rb.acked {
			continue // already acknowledged
		}
		var env event.Envelope
		if json.Unmarshal(sc.Bytes(), &env) == nil {
			out = append(out, env)
		}
	}
	return out, sc.Err()
}

// Ack advances the acknowledged cursor by n delivered envelopes and persists it.
// When the entire buffer has been acked, the buffer file is compacted (emptied).
func (rb *RingBuffer) Ack(n int) error {
	rb.mu.Lock()
	defer rb.mu.Unlock()
	rb.acked += int64(n)
	if err := rb.persistCursor(); err != nil {
		return err
	}
	return rb.maybeCompact()
}

func (rb *RingBuffer) persistCursor() error {
	b, _ := json.Marshal(rb.acked)
	return os.WriteFile(rb.cursor, b, 0o600)
}

// maybeCompact truncates the buffer once everything is acked, resetting the
// cursor. Keeps the on-disk file from growing without bound.
func (rb *RingBuffer) maybeCompact() error {
	total, err := rb.lineCount()
	if err != nil {
		return err
	}
	if total > 0 && rb.acked >= total {
		if err := os.Truncate(rb.path, 0); err != nil {
			return err
		}
		rb.acked = 0
		return rb.persistCursor()
	}
	return nil
}

func (rb *RingBuffer) lineCount() (int64, error) {
	f, err := os.Open(rb.path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	var n int64
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		n++
	}
	return n, sc.Err()
}
