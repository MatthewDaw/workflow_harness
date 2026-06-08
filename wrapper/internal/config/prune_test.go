package config

import (
	"os"
	"path/filepath"
	"testing"
)

// TestPruneToEffective proves the tight-mirror prune: a root skill/agent NOT in
// HQ's effective set is deleted, while one that IS in the set — or is provided by
// the connected repo's own .claude — is kept.
func TestPruneToEffective(t *testing.T) {
	home := t.TempDir()
	root := filepath.Join(home, ".claude+", "roots", "demo-1234") // must contain /roots/
	mkSkill := func(name string) {
		d := filepath.Join(root, "skills", name)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "SKILL.md"), []byte("---\nname: "+name+"\n---\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mkAgent := func(name string) {
		if err := os.MkdirAll(filepath.Join(root, "agents"), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, "agents", name+".md"), []byte("---\nname: "+name+"\n---\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mkSkill("keep-hq")     // in the effective set
	mkSkill("stale-hq")    // not in the effective set -> pruned
	mkSkill("repo-local")  // provided by the repo's .claude -> kept
	mkAgent("keep-agent")  // in the effective set
	mkAgent("stale-agent") // pruned

	// The connected repo provides "repo-local" as a project skill.
	repo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repo, ".claude", "skills", "repo-local"), 0o755); err != nil {
		t.Fatal(err)
	}

	remote := []RemoteItem{
		{Kind: KindSkill, Name: "keep-hq"},
		{Kind: KindAgent, Name: "keep-agent"},
	}
	removed, errs := PruneToEffective(root, repo, remote)
	if len(errs) != 0 {
		t.Fatalf("unexpected errs: %v", errs)
	}
	if removed != 2 {
		t.Fatalf("removed = %d, want 2 (stale-hq skill + stale-agent)", removed)
	}

	exists := func(p ...string) bool { _, err := os.Stat(filepath.Join(append([]string{root}, p...)...)); return err == nil }
	if !exists("skills", "keep-hq") {
		t.Error("keep-hq (in effective set) was deleted")
	}
	if exists("skills", "stale-hq") {
		t.Error("stale-hq (not in effective set) survived")
	}
	if !exists("skills", "repo-local") {
		t.Error("repo-local (provided by the repo) was deleted")
	}
	if !exists("agents", "keep-agent.md") {
		t.Error("keep-agent was deleted")
	}
	if exists("agents", "stale-agent.md") {
		t.Error("stale-agent survived")
	}
}

// TestPruneRefusesNonRoot proves the safety guard: a path that is not a
// per-project root (e.g. ~/.claude) is refused outright — nothing is deleted.
func TestPruneRefusesNonRoot(t *testing.T) {
	home := t.TempDir()
	bad := filepath.Join(home, ".claude") // no /roots/ segment
	if err := os.MkdirAll(filepath.Join(bad, "skills", "x"), 0o755); err != nil {
		t.Fatal(err)
	}
	removed, errs := PruneToEffective(bad, "", nil)
	if removed != 0 || len(errs) == 0 {
		t.Fatalf("prune must refuse a non-root path; removed=%d errs=%v", removed, errs)
	}
	if _, err := os.Stat(filepath.Join(bad, "skills", "x")); err != nil {
		t.Error("prune deleted from a refused (non-root) path")
	}
}
