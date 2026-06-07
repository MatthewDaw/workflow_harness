package topic

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestNearestDocCoversTouchedFile: a touched file whose stem appears in a feature
// doc filename resolves that doc (ref + contents).
func TestNearestDocCoversTouchedFile(t *testing.T) {
	repo := t.TempDir()
	featDir := filepath.Join(repo, "docs", "plans", "features")
	if err := os.MkdirAll(featDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := "the login feature doc body"
	if err := os.WriteFile(filepath.Join(featDir, "login_page.html"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	ref, contents := NearestDoc(repo, []string{"src/screens/login.tsx"})
	if ref != "docs/plans/features/login_page.html" {
		t.Errorf("docRef = %q", ref)
	}
	if contents != body {
		t.Errorf("contents = %q", contents)
	}
}

// TestNearestDocNoneWhenNoCoverage: an unrelated touched file with no PRD yields
// no doc (R6: bias to no-doc).
func TestNearestDocNoneWhenNoCoverage(t *testing.T) {
	repo := t.TempDir()
	ref, contents := NearestDoc(repo, []string{"src/util/zzqq.go"})
	if ref != "" || contents != "" {
		t.Errorf("expected no doc, got ref=%q contents=%q", ref, contents)
	}
}

// TestNearestDocPRDFallback: with no covering feature doc but a PRD present, the
// PRD is used as the broad fallback.
func TestNearestDocPRDFallback(t *testing.T) {
	repo := t.TempDir()
	docs := filepath.Join(repo, "docs")
	if err := os.MkdirAll(docs, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(docs, "PRD.html"), []byte("prd body"), 0o644); err != nil {
		t.Fatal(err)
	}
	ref, contents := NearestDoc(repo, []string{"src/whatever.go"})
	if ref != "docs/PRD.html" || contents != "prd body" {
		t.Errorf("PRD fallback failed: ref=%q contents=%q", ref, contents)
	}
}

// TestBuildTranscriptSlicePositionAware: the slice carries the user prompt (head)
// and the tail of the assistant turn, each bounded.
func TestBuildTranscriptSlice(t *testing.T) {
	// Use 'Z' as the filler: it appears in neither the "user:"/"assistant:" labels
	// nor the prompt, so its count isolates the clipped assistant tail.
	long := strings.Repeat("Z", maxAssistantChars+500)
	s := BuildTranscriptSlice("fix the redirect", long)
	if !strings.Contains(s, "user: fix the redirect") {
		t.Error("slice missing the user prompt head")
	}
	if !strings.HasPrefix(s, "user:") {
		t.Error("user prompt should lead the slice")
	}
	// Assistant side is the TAIL, clipped to the cap.
	if strings.Count(s, "Z") != maxAssistantChars {
		t.Errorf("assistant tail not clipped to cap: %d filler runes, want %d", strings.Count(s, "Z"), maxAssistantChars)
	}
}
