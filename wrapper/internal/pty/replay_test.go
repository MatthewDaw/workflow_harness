package pty

import (
	"bytes"
	"testing"
)

// TestSessionHistoryReplay proves AddSink replays an existing session's recent
// output to a newly-registered sink — the fix that stops a freshly-attached
// client (or a `+ new` session) from rendering blank until the next repaint.
func TestSessionHistoryReplay(t *testing.T) {
	s := &Session{ID: "s1"}
	s.recordHist([]byte("hello "))
	s.recordHist([]byte("world"))
	if got := string(s.History()); got != "hello world" {
		t.Fatalf("History() = %q, want %q", got, "hello world")
	}

	m := NewMux("", 80, 24, nil)
	m.sessions = []*Session{s}

	var got []byte
	m.AddSink("c1", func(id string, b []byte) {
		if id == "s1" {
			got = append(got, b...)
		}
	})
	if string(got) != "hello world" {
		t.Fatalf("replay to new sink = %q, want %q", got, "hello world")
	}
}

// TestSessionHistoryTrim proves the replay buffer is capped at maxHist, keeping
// the most recent bytes.
func TestSessionHistoryTrim(t *testing.T) {
	s := &Session{ID: "s"}
	s.recordHist(bytes.Repeat([]byte("a"), maxHist))
	s.recordHist([]byte("TAIL"))
	h := s.History()
	if len(h) != maxHist {
		t.Fatalf("history len = %d, want %d", len(h), maxHist)
	}
	if !bytes.HasSuffix(h, []byte("TAIL")) {
		t.Fatalf("trimmed history lost the most recent bytes; suffix = %q", h[len(h)-4:])
	}
}
