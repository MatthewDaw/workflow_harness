package capture

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// TestTailerEmitsOrderedEvents is the characterization test against a recorded
// transcript fixture (U14 execution note). It asserts the emitted event kinds,
// order, and key fields.
func TestTailerEmitsOrderedEvents(t *testing.T) {
	var got []event.Event
	var firstTurn string
	tail := NewTailer("a91f", "testdata/transcript.jsonl",
		func(e event.Event) { got = append(got, e) },
		func(_, text string) { firstTurn = text },
	)
	if err := tail.Poll(); err != nil {
		t.Fatalf("poll: %v", err)
	}

	wantKinds := []event.Kind{
		event.KindUserMsg, event.KindToolCall, event.KindToolResult,
		event.KindAssistantMsg, event.KindCostTick,
	}
	if len(got) != len(wantKinds) {
		t.Fatalf("want %d events, got %d: %+v", len(wantKinds), len(got), got)
	}
	for i, k := range wantKinds {
		if got[i].Kind != k {
			t.Errorf("event %d: want %s got %s", i, k, got[i].Kind)
		}
		if err := got[i].Validate(); err != nil {
			t.Errorf("event %d invalid: %v", i, err)
		}
	}
	if firstTurn != "add reconciliation view" {
		t.Errorf("first turn = %q", firstTurn)
	}
	// tool.call should summarize the file path.
	if got[1].ArgsSummary != "src/state/weeklyLifecycle.ts" {
		t.Errorf("argsSummary = %q", got[1].ArgsSummary)
	}
	// cost.tick delta should equal the cumulative cost on the first result.
	if got[4].DeltaUsd == nil || *got[4].DeltaUsd != 0.62 {
		t.Errorf("cost delta = %v", got[4].DeltaUsd)
	}
}

// TestTailerNoDoubleEmit verifies a second Poll with no new bytes emits nothing.
func TestTailerNoDoubleEmit(t *testing.T) {
	var count int
	tail := NewTailer("a91f", "testdata/transcript.jsonl",
		func(event.Event) { count++ }, nil)
	_ = tail.Poll()
	first := count
	_ = tail.Poll()
	if count != first {
		t.Errorf("re-poll double-emitted: %d -> %d", first, count)
	}
}

// TestTailerPartialLine verifies a partial trailing line is not emitted until
// completed by a later append.
func TestTailerPartialLine(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "s.jsonl")
	// Write a complete line plus a partial one.
	full := `{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}` + "\n"
	if err := os.WriteFile(p, []byte(full+`{"type":"tool_use","name":"Read"`), 0o600); err != nil {
		t.Fatal(err)
	}
	var got []event.Event
	tail := NewTailer("s", p, func(e event.Event) { got = append(got, e) }, nil)
	_ = tail.Poll()
	if len(got) != 1 {
		t.Fatalf("only the complete line should emit, got %d", len(got))
	}
	// Complete the partial line.
	f, _ := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0o600)
	_, _ = f.WriteString(`,"input":{"file_path":"x.go"}}` + "\n")
	f.Close()
	_ = tail.Poll()
	if len(got) != 2 || got[1].Kind != event.KindToolCall {
		t.Fatalf("completed line should emit tool.call, got %+v", got)
	}
}

func TestMapHook(t *testing.T) {
	e, ok := MapHook(HookEvent{HookEventName: "Notification", SessionID: "s"}, event.StatusActive)
	if !ok || e.To != event.StatusNeedsInput {
		t.Errorf("Notification should map to needs_input, got %+v ok=%v", e, ok)
	}
	if _, ok := MapHook(HookEvent{HookEventName: "PostToolUse", SessionID: "s"}, event.StatusActive); ok {
		t.Error("PostToolUse should not produce a status change")
	}
}

func TestInstallHooksIdempotent(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home) // windows home
	p1, err := InstallHooks("claude-plus __hook")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	p2, err := InstallHooks("claude-plus __hook")
	if err != nil {
		t.Fatalf("re-install: %v", err)
	}
	if p1 != p2 {
		t.Fatalf("settings path drifted")
	}
	// Hooks must be written into the isolated claude+ config root (~/.claude+),
	// not ~/.claude, since claude+ launches Claude with CLAUDE_CONFIG_DIR there.
	wantDir := filepath.Join(home, ".claude+")
	if filepath.Dir(p1) != wantDir {
		t.Fatalf("hooks installed at %q, want under %q", p1, wantDir)
	}
	b, _ := os.ReadFile(p1)
	// Re-install must not duplicate our managed entry under Notification.
	// A crude check: the command should appear exactly 5 times (one per managed
	// event: PreToolUse, PostToolUse, Stop, Notification, UserPromptSubmit).
	got := countOccurrences(string(b), "claude-plus __hook")
	if got != 5 {
		t.Errorf("expected 5 managed hook commands, got %d", got)
	}
}

func countOccurrences(s, sub string) int {
	n := 0
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			n++
		}
	}
	return n
}
