package workflow

import (
	"context"
	"strings"
	"sync"
	"testing"
)

// stubClaude swaps the runClaude seam for the duration of a test and restores it
// after, so the loop/cap logic is exercised without spawning the real CLI.
func stubClaude(t *testing.T, fn func(ctx context.Context, repoRoot, prompt string) (string, error)) {
	t.Helper()
	orig := runClaude
	runClaude = fn
	t.Cleanup(func() { runClaude = orig })
}

// TestRunNodeComposesDepOutputs proves runNode folds the dependency outputs into
// the prompt and returns the (trimmed) captured stdout.
func TestRunNodeComposesDepOutputs(t *testing.T) {
	var gotPrompt string
	stubClaude(t, func(_ context.Context, _, prompt string) (string, error) {
		gotPrompt = prompt
		return "  node result  \n", nil
	})
	node := Node{ID: "b", Prompt: "do the thing", DependsOn: []string{"a"}}
	out, err := runNode(context.Background(), "/repo", node, map[string]string{"a": "upstream output"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "node result" {
		t.Fatalf("out = %q, want trimmed 'node result'", out)
	}
	if !strings.Contains(gotPrompt, "upstream output") {
		t.Fatalf("prompt missing dependency output: %q", gotPrompt)
	}
	if !strings.Contains(gotPrompt, "do the thing") {
		t.Fatalf("prompt missing node task: %q", gotPrompt)
	}
}

// TestEvalRerunSelfStopsOnDone proves the self-judge loop stops as soon as the
// judge returns DONE — here on the very first check, so the node runs exactly once.
func TestEvalRerunSelfStopsOnDone(t *testing.T) {
	var calls int
	stubClaude(t, func(_ context.Context, _, prompt string) (string, error) {
		calls++
		if strings.Contains(prompt, "DONE or CONTINUE") {
			return "DONE", nil
		}
		return "regenerated", nil
	})
	node := Node{ID: "n", Prompt: "gen", Rerun: &Rerun{Mode: "self", EndCriteria: "looks good", MaxRuns: 5}}
	final, runs, done, err := evalRerun(context.Background(), "/repo", node, nil, "first output", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !done {
		t.Fatal("expected done=true on first DONE verdict")
	}
	if runs != 1 {
		t.Fatalf("runs = %d, want 1 (node already ran once, judge said DONE immediately)", runs)
	}
	if final != "first output" {
		t.Fatalf("final = %q, want 'first output'", final)
	}
}

// TestEvalRerunSelfHitsCap proves a judge that never says DONE loops until MaxRuns
// and reports done=false (so the caller fails the node) instead of looping forever.
func TestEvalRerunSelfHitsCap(t *testing.T) {
	var nodeRuns int
	stubClaude(t, func(_ context.Context, _, prompt string) (string, error) {
		if strings.Contains(prompt, "DONE or CONTINUE") {
			return "CONTINUE", nil
		}
		nodeRuns++
		return "again", nil
	})
	node := Node{ID: "n", Prompt: "gen", Rerun: &Rerun{Mode: "self", EndCriteria: "never", MaxRuns: 3}}
	_, runs, done, err := evalRerun(context.Background(), "/repo", node, nil, "first output", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if done {
		t.Fatal("expected done=false when the cap is hit unsatisfied")
	}
	if runs != 3 {
		t.Fatalf("runs = %d, want 3 (MaxRuns cap)", runs)
	}
	// The node re-ran twice after the initial run (runs 2 and 3).
	if nodeRuns != 2 {
		t.Fatalf("node re-runs = %d, want 2", nodeRuns)
	}
}

// TestEvalRerunDeclaredBy proves the declared-by loop runs the checker node and
// stops when the checker declares DONE — here after one regeneration.
func TestEvalRerunDeclaredBy(t *testing.T) {
	var mu sync.Mutex
	var checkerCalls int
	stubClaude(t, func(_ context.Context, _, prompt string) (string, error) {
		// The checker prompt carries the checker's own prompt text "review it".
		if strings.Contains(prompt, "review it") {
			mu.Lock()
			checkerCalls++
			n := checkerCalls
			mu.Unlock()
			if n >= 2 {
				return "DONE", nil
			}
			return "CONTINUE", nil
		}
		return "regenerated", nil
	})
	gen := Node{ID: "gen", Prompt: "generate", Rerun: &Rerun{Mode: "declared-by", DeclaredBy: "chk", MaxRuns: 5}}
	chk := Node{ID: "chk", Prompt: "review it"}
	byID := map[string]Node{"gen": gen, "chk": chk}
	_, runs, done, err := evalRerun(context.Background(), "/repo", gen, byID, "first output", nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !done {
		t.Fatal("expected done=true once the checker declared DONE")
	}
	if runs != 2 {
		t.Fatalf("runs = %d, want 2 (initial + one regeneration before DONE)", runs)
	}
}

// TestParseVerdict proves DONE is recognized only on an unambiguous DONE token.
func TestParseVerdict(t *testing.T) {
	cases := map[string]bool{
		"DONE":                          true,
		"done":                          true,
		"The work is DONE.":             true,
		"CONTINUE":                      false,
		"":                              false,
		"not finished, please continue": false,
		// Ambiguous output mentioning both → CONTINUE (never a false DONE).
		"DONE? no — CONTINUE": false,
	}
	for in, want := range cases {
		if got := parseVerdict(in); got != want {
			t.Errorf("parseVerdict(%q) = %v, want %v", in, got, want)
		}
	}
}
