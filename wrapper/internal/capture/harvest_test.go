package capture

import (
	"os"
	"path/filepath"
	"testing"
)

// writeClassToolLine builds an assistant row carrying one write-class tool_use
// with the given file path under input.file_path.
func writeClassToolLine(tool, file string) string {
	return `{"type":"assistant","message":{"role":"assistant","content":[` +
		`{"type":"tool_use","name":"` + tool + `","input":{"file_path":` + jsonQuote(file) + `}}]}}` + "\n"
}

func assistantTextLine(text string) string {
	return `{"type":"assistant","message":{"role":"assistant","content":[` +
		`{"type":"text","text":` + jsonQuote(text) + `}]}}` + "\n"
}

// TestHarvestTurnWriteClassFiles re-parses a turn's raw JSONL and recovers ONLY
// the write-class touched files (Edit/Write/MultiEdit/NotebookEdit), de-duped,
// plus the latest user prompt and the assistant tail.
func TestHarvestTurnWriteClassFiles(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "t.jsonl")
	content := userLine("fix the redirect") +
		writeClassToolLine("Edit", "src/login.tsx") +
		writeClassToolLine("Read", "src/ignore-me.go") + // not write-class
		writeClassToolLine("Write", "src/router.tsx") +
		writeClassToolLine("Edit", "src/login.tsx") + // dup, collapsed
		assistantTextLine("done, redirect now points to /home")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	h, err := HarvestTurn(path, 0)
	if err != nil {
		t.Fatalf("HarvestTurn: %v", err)
	}
	if len(h.FilesTouched) != 2 {
		t.Fatalf("FilesTouched = %v, want 2 unique write-class files", h.FilesTouched)
	}
	if h.FilesTouched[0] != "src/login.tsx" || h.FilesTouched[1] != "src/router.tsx" {
		t.Errorf("FilesTouched order/content = %v", h.FilesTouched)
	}
	if h.LatestUserPrompt != "fix the redirect" {
		t.Errorf("LatestUserPrompt = %q", h.LatestUserPrompt)
	}
	if h.AssistantTail != "done, redirect now points to /home" {
		t.Errorf("AssistantTail = %q", h.AssistantTail)
	}
	if h.EndOffset != int64(len(content)) {
		t.Errorf("EndOffset = %d, want %d", h.EndOffset, len(content))
	}
}

// TestHarvestTurnFromOffset confirms the harvest reads ONLY rows appended after
// the caller-held offset (so the next turn re-parses just its own rows).
func TestHarvestTurnFromOffset(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "t.jsonl")
	first := userLine("turn one") + writeClassToolLine("Edit", "a.go")
	if err := os.WriteFile(path, []byte(first), 0o644); err != nil {
		t.Fatal(err)
	}
	h1, _ := HarvestTurn(path, 0)
	if len(h1.FilesTouched) != 1 || h1.FilesTouched[0] != "a.go" {
		t.Fatalf("turn1 files = %v", h1.FilesTouched)
	}

	// Append a second turn and harvest from the first turn's end offset.
	second := userLine("turn two") + writeClassToolLine("Write", "b.go")
	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o644)
	_, _ = f.WriteString(second)
	_ = f.Close()

	h2, _ := HarvestTurn(path, h1.EndOffset)
	if len(h2.FilesTouched) != 1 || h2.FilesTouched[0] != "b.go" {
		t.Fatalf("turn2 from offset should see only b.go, got %v", h2.FilesTouched)
	}
	if h2.LatestUserPrompt != "turn two" {
		t.Errorf("turn2 prompt = %q", h2.LatestUserPrompt)
	}
}

// TestHarvestNativeTitle harvests Claude Code's customTitle as a bonus signal.
func TestHarvestNativeTitle(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "t.jsonl")
	content := `{"type":"ai-title","customTitle":"refactor auth flow"}` + "\n" +
		userLine("hello")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	h, _ := HarvestTurn(path, 0)
	if h.NativeTitle != "refactor auth flow" {
		t.Errorf("NativeTitle = %q", h.NativeTitle)
	}
}

// TestHarvestMissingFile yields a zero harvest (offset preserved) — no error.
func TestHarvestMissingFile(t *testing.T) {
	h, err := HarvestTurn(filepath.Join(t.TempDir(), "nope.jsonl"), 42)
	if err != nil {
		t.Fatalf("missing file should not error: %v", err)
	}
	if h.EndOffset != 42 || len(h.FilesTouched) != 0 {
		t.Errorf("expected zero harvest at offset 42, got %+v", h)
	}
}
