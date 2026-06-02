package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDiffClassifies(t *testing.T) {
	local := []Item{
		{Kind: KindAgent, Name: "builder", Hash: "h1"},   // matches HQ -> in sync
		{Kind: KindAgent, Name: "local-only", Hash: "h2"}, // needs push
		{Kind: KindSkill, Name: "drifty", Hash: "hX"},     // differs
		{Kind: KindSkill, Name: "broken", Err: "empty definition"},
	}
	remote := []RemoteItem{
		{Kind: KindAgent, Name: "builder", Scope: "org", Hash: "h1"},
		{Kind: KindSkill, Name: "drifty", Scope: "user#u", Hash: "hY"},
		{Kind: KindAgent, Name: "hq-only", Scope: "proj#p", Hash: "h9"}, // needs pull
	}
	r := Diff(local, remote)
	if r.NeedsPush != 1 || r.NeedsPull != 1 || r.Differs != 1 || r.Errors != 1 {
		t.Fatalf("unexpected counts: %+v", r)
	}
	if r.InSync() {
		t.Error("report with drift should not be in sync")
	}
}

func TestDiffInSync(t *testing.T) {
	local := []Item{{Kind: KindAgent, Name: "a", Hash: "h"}}
	remote := []RemoteItem{{Kind: KindAgent, Name: "a", Hash: "h", Scope: "user#u"}}
	if !Diff(local, remote).InSync() {
		t.Error("identical sets should be in sync")
	}
}

func TestReadLocalReportsMalformed(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	agents := filepath.Join(home, ".claude", "agents")
	if err := os.MkdirAll(agents, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(agents, "good.md"), []byte("# Good agent\nbody"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(agents, "empty.md"), []byte("   "), 0o644); err != nil {
		t.Fatal(err)
	}
	items, err := ReadLocal()
	if err != nil {
		t.Fatalf("ReadLocal: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("want 2 items, got %d", len(items))
	}
	var sawErr bool
	for _, it := range items {
		if it.Name == "empty" && it.Err != "" {
			sawErr = true
		}
	}
	if !sawErr {
		t.Error("empty agent should be flagged, not crash")
	}
}

func TestHashIgnoresLineEndings(t *testing.T) {
	if hashContent([]byte("a\r\nb")) != hashContent([]byte("a\nb")) {
		t.Error("CRLF/LF should hash equal")
	}
}
