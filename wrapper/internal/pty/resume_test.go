package pty

import (
	"runtime"
	"testing"
)

// TestResumeSpecSwapsSessionIDForResume proves the resume transform rewrites a
// fresh `--session-id <id>` launch (as DefaultSpawn produces) into `--resume
// <id>` so the child reattaches to the existing Claude conversation, while
// preserving any trailing flags (e.g. --dangerously-skip-permissions) and the
// rest of the spec (Dir/Env/Isolate).
func TestResumeSpecSwapsSessionIDForResume(t *testing.T) {
	const id = "11111111-2222-3333-4444-555555555555"
	t.Setenv("CLAUDE_PLUS_DANGEROUS", "1")
	fresh := DefaultSpawn("/repo", id)
	if !hasFlagValue(fresh.Args, "--session-id", id) {
		t.Fatalf("precondition: DefaultSpawn must emit --session-id; got %v", fresh.Args)
	}

	res := toResumeSpec(fresh, id)
	if hasFlagValue(res.Args, "--session-id", id) {
		t.Fatalf("resume spec must NOT carry --session-id; got %v", res.Args)
	}
	if !hasFlagValue(res.Args, "--resume", id) {
		t.Fatalf("resume spec must carry --resume %s; got %v", id, res.Args)
	}
	// Trailing dangerous flag preserved.
	found := false
	for _, a := range res.Args {
		if a == "--dangerously-skip-permissions" {
			found = true
		}
	}
	if !found {
		t.Fatalf("resume spec must preserve --dangerously-skip-permissions; got %v", res.Args)
	}
	if res.Dir != fresh.Dir || res.Isolate != fresh.Isolate {
		t.Fatalf("resume spec must preserve Dir/Isolate; got %+v vs %+v", res, fresh)
	}
}

// TestResumeSpecFallbackPrepends proves a fake/test spec without a leading
// --session-id pair still resumes (flag prepended) without losing its args.
func TestResumeSpecFallbackPrepends(t *testing.T) {
	const id = "abc"
	spec := CmdSpec{Name: "echo", Args: []string{"hi"}}
	res := toResumeSpec(spec, id)
	if !hasFlagValue(res.Args, "--resume", id) {
		t.Fatalf("fallback must prepend --resume %s; got %v", id, res.Args)
	}
	if len(res.Args) != 3 || res.Args[2] != "hi" {
		t.Fatalf("fallback must keep original args; got %v", res.Args)
	}
}

// TestSpawnResumedKeepsIDAndPumps proves SpawnResumed creates a session under
// the SAME id (no new UUID) in resume mode, appends it in order, and that
// SeedNaming restores the naming latches so later auto-naming/titling is locked
// out. It uses a cross-platform fake child so no real claude is needed.
func TestSpawnResumedKeepsIDAndPumps(t *testing.T) {
	spawn := func(repoRoot, sessionID string) CmdSpec {
		if runtime.GOOS == "windows" {
			return CmdSpec{Name: "cmd", Args: []string{"/c", "echo hi"}}
		}
		return CmdSpec{Name: "sh", Args: []string{"-c", "echo hi"}}
	}
	m := NewMux("", 80, 24, spawn)
	defer m.CloseAll()

	const id = "restored-id-1"
	s, err := m.SpawnResumed(id, "restored-name")
	if err != nil {
		t.Fatalf("SpawnResumed: %v", err)
	}
	if s.ID != id {
		t.Fatalf("SpawnResumed must keep the same id %q; got %q", id, s.ID)
	}
	if m.Get(id) == nil {
		t.Fatalf("resumed session must be registered in the mux")
	}

	s.SeedNaming("restored-name", true, true, true)
	// With all latches set, an auto-name attempt must be a no-op.
	if renamed, _ := m.ApplyAutoName(id, "some first user turn"); renamed {
		t.Fatalf("SeedNaming(...,manualName=true) must lock out auto-naming")
	}
	if renamed, _ := m.ApplyTitle(id, "Some LLM Title"); renamed {
		t.Fatalf("SeedNaming(...,titleSet/manual) must lock out auto-titling")
	}
	if got := s.Name; got != "restored-name" {
		t.Fatalf("name must remain the restored value; got %q", got)
	}
}
