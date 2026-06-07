package capture

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// userLine builds a minimal transcript JSONL row for a plain-string user prompt.
func userLine(text string) string {
	return `{"type":"user","message":{"role":"user","content":` + jsonQuote(text) + "}}\n"
}

func jsonQuote(s string) string {
	out := []byte{'"'}
	for _, r := range s {
		switch r {
		case '"':
			out = append(out, '\\', '"')
		case '\\':
			out = append(out, '\\', '\\')
		default:
			out = append(out, string(r)...)
		}
	}
	return string(append(out, '"'))
}

// collectSink returns an EventSink that appends every event's kind to a slice
// guarded by a mutex (Poll may run on its own goroutine).
func collectSink() (EventSink, func() []string) {
	var mu sync.Mutex
	var kinds []string
	sink := func(e event.Event) {
		mu.Lock()
		kinds = append(kinds, string(e.Kind))
		mu.Unlock()
	}
	get := func() []string {
		mu.Lock()
		defer mu.Unlock()
		out := make([]string, len(kinds))
		copy(out, kinds)
		return out
	}
	return sink, get
}

// TestPollEmitsOncePerLine verifies a complete line emits exactly once and a
// re-Poll with no new bytes emits nothing (monotonic offset).
func TestPollEmitsOncePerLine(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s.jsonl")
	if err := os.WriteFile(path, []byte(userLine("hello world")), 0o600); err != nil {
		t.Fatal(err)
	}
	sink, get := collectSink()
	tl := NewTailer("tab1", path, sink, nil)

	if err := tl.Poll(); err != nil {
		t.Fatalf("poll: %v", err)
	}
	if got := get(); len(got) != 1 {
		t.Fatalf("want 1 event, got %v", got)
	}
	if err := tl.Poll(); err != nil {
		t.Fatalf("poll2: %v", err)
	}
	if got := get(); len(got) != 1 {
		t.Fatalf("re-poll should add nothing, got %v", got)
	}
}

// TestPartialTrailingLine verifies a line without a trailing newline is not
// emitted until it is completed by a later append.
func TestPartialTrailingLine(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s.jsonl")
	// Write a complete line plus a partial (no newline) second line.
	partial := userLine("first")
	partial += `{"type":"user","message":{"role":"user","content":"sec`
	if err := os.WriteFile(path, []byte(partial), 0o600); err != nil {
		t.Fatal(err)
	}
	sink, get := collectSink()
	tl := NewTailer("tab1", path, sink, nil)

	if err := tl.Poll(); err != nil {
		t.Fatal(err)
	}
	if got := get(); len(got) != 1 {
		t.Fatalf("partial line should not emit; want 1 event, got %v", got)
	}

	// Complete the second line.
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString(`ond"}}` + "\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()

	if err := tl.Poll(); err != nil {
		t.Fatal(err)
	}
	if got := get(); len(got) != 2 {
		t.Fatalf("completed line should now emit; want 2 events, got %v", got)
	}
}

// TestRepointStreamsNewFile verifies that after Repoint the tailer follows the
// new file from its start while keeping the stable sessID on emitted events.
func TestRepointStreamsNewFile(t *testing.T) {
	dir := t.TempDir()
	oldPath := filepath.Join(dir, "tab.jsonl")
	newPath := filepath.Join(dir, "live.jsonl")
	if err := os.WriteFile(oldPath, []byte(userLine("from-old")), 0o600); err != nil {
		t.Fatal(err)
	}

	var mu sync.Mutex
	var sids []string
	sink := func(e event.Event) {
		mu.Lock()
		sids = append(sids, e.SessionID)
		mu.Unlock()
	}
	tl := NewTailer("tab1", oldPath, sink, nil)
	if err := tl.Poll(); err != nil {
		t.Fatal(err)
	}

	// Resume diverges: live transcript appears at newPath.
	if err := os.WriteFile(newPath, []byte(userLine("from-live")), 0o600); err != nil {
		t.Fatal(err)
	}
	tl.Repoint(newPath)
	if err := tl.Poll(); err != nil {
		t.Fatal(err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(sids) != 2 {
		t.Fatalf("want 2 events (old + live), got %d", len(sids))
	}
	for _, s := range sids {
		if s != "tab1" {
			t.Fatalf("events must stay keyed on stable tab id, got %q", s)
		}
	}
}

// TestRepointConcurrentWithPoll exercises the mutex: Poll on one goroutine and
// Repoint on another must not race (run under -race).
func TestRepointConcurrentWithPoll(t *testing.T) {
	dir := t.TempDir()
	a := filepath.Join(dir, "a.jsonl")
	b := filepath.Join(dir, "b.jsonl")
	if err := os.WriteFile(a, []byte(userLine("a")), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(b, []byte(userLine("b")), 0o600); err != nil {
		t.Fatal(err)
	}
	sink, _ := collectSink()
	tl := NewTailer("tab1", a, sink, nil)

	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				_ = tl.Poll()
			}
		}
	}()
	for i := 0; i < 200; i++ {
		if i%2 == 0 {
			tl.Repoint(b)
		} else {
			tl.Repoint(a)
		}
	}
	time.Sleep(5 * time.Millisecond)
	close(stop)
	wg.Wait()
}
