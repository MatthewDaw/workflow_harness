package capture

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// TestTailerEmitsCurrentSchemaContent is the ground-truth proof: a REAL,
// current-format claude+ transcript (testdata/transcript_current.jsonl) is fed
// through the tailer and we assert that EVERY meaningful content block is
// emitted WITH its real content — user text, assistant text, every tool_use, and
// every tool_result — and that control/metadata rows are skipped (never crash).
//
// This FAILS on the old jsonl.go (which expected top-level tool_use/tool_result/
// result rows and emitted only a coarse, mostly-empty subset) and PASSES on the
// current-schema parser.
func TestTailerEmitsCurrentSchemaContent(t *testing.T) {
	var got []event.Event
	var firstTurn string
	var exU, exA string
	tail := NewTailer("a24c", "testdata/transcript_current.jsonl",
		func(e event.Event) { got = append(got, e) },
		func(_, text string) { firstTurn = text },
	).OnExchange(func(_, u, a string) { exU, exA = u, a })
	if err := tail.Poll(); err != nil {
		t.Fatalf("poll: %v", err)
	}

	// Tally by kind. The fixture is a real 56-row transcript slice whose
	// emittable content is: 3 user.msg, 7 assistant.msg, 8 tool.call, 8
	// tool.result (24 control rows are skipped). See the fixture-builder report.
	counts := map[event.Kind]int{}
	for _, e := range got {
		counts[e.Kind]++
		if err := e.Validate(); err != nil {
			t.Errorf("emitted event invalid: %v (%+v)", err, e)
		}
	}
	want := map[event.Kind]int{
		event.KindUserMsg:     3,
		event.KindAssistantMsg: 7,
		event.KindToolCall:    8,
		event.KindToolResult:  8,
	}
	for k, n := range want {
		if counts[k] != n {
			t.Errorf("kind %s: want %d, got %d (total %d)", k, n, counts[k], len(got))
		}
	}

	// EVERY user/assistant message event must carry real text content (not empty).
	for _, e := range got {
		if e.Kind == event.KindUserMsg || e.Kind == event.KindAssistantMsg {
			if e.Text == "" {
				t.Errorf("%s emitted with empty text — content was dropped", e.Kind)
			}
		}
		if e.Kind == event.KindToolCall && e.Tool == "" {
			t.Errorf("tool.call emitted with no tool name")
		}
	}

	// First-turn auto-name + first-exchange title hooks still fire with content.
	if firstTurn == "" {
		t.Error("first-turn hook never fired")
	}
	if exU == "" || exA == "" {
		t.Errorf("first-exchange hook missing content: user=%q assistant=%q", exU, exA)
	}

	// At least one tool.call carries a real command/path summary, and at least one
	// tool.result carries real output text.
	sawArgs, sawResult := false, false
	for _, e := range got {
		if e.Kind == event.KindToolCall && e.ArgsSummary != "" {
			sawArgs = true
		}
		if e.Kind == event.KindToolResult && e.Summary != "" {
			sawResult = true
		}
	}
	if !sawArgs {
		t.Error("no tool.call carried an args summary")
	}
	if !sawResult {
		t.Error("no tool.result carried summary content")
	}
}

// TestTailerCurrentSchemaUnits exercises the block-walking on a minimal hand-built
// current-schema transcript so the multi-event-per-row expansion is pinned
// precisely: one assistant row with text + two tool_use blocks → 3 events; one
// user row with two tool_result blocks → 2 events; a control row → 0.
func TestTailerCurrentSchemaUnits(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "s.jsonl")
	lines := []string{
		`{"type":"user","message":{"role":"user","content":"do the thing"}}`,
		`{"type":"mode","mode":"normal"}`,
		`{"type":"assistant","message":{"role":"assistant","usage":{"output_tokens":42},"content":[{"type":"thinking","thinking":"hmm"},{"type":"text","text":"On it."},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls -la"}},{"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"a.go"}}]}}`,
		`{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"total 8"},{"type":"tool_result","tool_use_id":"t2","is_error":true,"content":[{"type":"text","text":"boom"}]}]}}`,
	}
	if err := os.WriteFile(p, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var got []event.Event
	tail := NewTailer("s", p, func(e event.Event) { got = append(got, e) }, nil)
	if err := tail.Poll(); err != nil {
		t.Fatal(err)
	}
	wantKinds := []event.Kind{
		event.KindUserMsg,                       // "do the thing"
		event.KindAssistantMsg,                  // "On it." (+42 tokens, thinking skipped)
		event.KindToolCall, event.KindToolCall,  // Bash, Read
		event.KindToolResult, event.KindToolResult, // t1 ok, t2 err
	}
	if len(got) != len(wantKinds) {
		t.Fatalf("want %d events, got %d: %+v", len(wantKinds), len(got), got)
	}
	for i, k := range wantKinds {
		if got[i].Kind != k {
			t.Fatalf("event %d: want %s got %s", i, k, got[i].Kind)
		}
	}
	if got[0].Text != "do the thing" {
		t.Errorf("user text = %q", got[0].Text)
	}
	if got[1].Text != "On it." || got[1].Tokens == nil || *got[1].Tokens != 42 {
		t.Errorf("assistant text/tokens = %q / %v", got[1].Text, got[1].Tokens)
	}
	if got[2].Tool != "Bash" || got[2].ArgsSummary != "ls -la" {
		t.Errorf("tool.call[0] = %q %q", got[2].Tool, got[2].ArgsSummary)
	}
	if got[3].Tool != "Read" || got[3].ArgsSummary != "a.go" {
		t.Errorf("tool.call[1] = %q %q", got[3].Tool, got[3].ArgsSummary)
	}
	if got[4].OK == nil || !*got[4].OK || got[4].Summary != "total 8" {
		t.Errorf("tool.result[0] ok/summary = %v %q", got[4].OK, got[4].Summary)
	}
	if got[5].OK == nil || *got[5].OK || got[5].Summary != "boom" {
		t.Errorf("tool.result[1] err/summary = %v %q", got[5].OK, got[5].Summary)
	}
}

// TestTailerNoDoubleEmit verifies a second Poll with no new bytes emits nothing.
func TestTailerNoDoubleEmit(t *testing.T) {
	var count int
	tail := NewTailer("a91f", "testdata/transcript_current.jsonl",
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
	// Write a complete line plus a partial one (current schema).
	full := `{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}` + "\n"
	partial := `{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"`
	if err := os.WriteFile(p, []byte(full+partial), 0o600); err != nil {
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
	_, _ = f.WriteString(`,"input":{"file_path":"x.go"}}]}}` + "\n")
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
	// Hooks are installed into the config root the daemon passes (the per-project
	// root in production); the test passes an explicit dir.
	dir := t.TempDir()
	p1, err := InstallHooks(dir, "claude-plus __hook")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	p2, err := InstallHooks(dir, "claude-plus __hook")
	if err != nil {
		t.Fatalf("re-install: %v", err)
	}
	if p1 != p2 {
		t.Fatalf("settings path drifted")
	}
	if filepath.Dir(p1) != dir {
		t.Fatalf("hooks installed at %q, want under %q", p1, dir)
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
