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

// fakeRemote is an in-memory RemoteSource for exercising ComputeDrift/Reconcile
// without a live HQ. Push mutates the set so a re-diff converges (idempotence);
// Body returns recorded content; bodyErr names one item whose Body() should fail
// (the malformed-but-non-fatal edge).
type fakeRemote struct {
	items   []RemoteItem
	bodies  map[string]string
	bodyErr string // "kind/name" whose Body() returns an error
}

func newFakeRemote() *fakeRemote { return &fakeRemote{bodies: map[string]string{}} }

func (f *fakeRemote) addRemote(kind Kind, name, body string) {
	key := string(kind) + "/" + name
	f.bodies[key] = body
	f.items = append(f.items, RemoteItem{Kind: kind, Name: name, Scope: "org", Hash: hashContent([]byte(body))})
}

func (f *fakeRemote) Fetch() ([]RemoteItem, error) {
	return append([]RemoteItem(nil), f.items...), nil
}

func (f *fakeRemote) Body(item RemoteItem) (string, error) {
	key := string(item.Kind) + "/" + item.Name
	if f.bodyErr == key {
		return "", os.ErrInvalid
	}
	if b, ok := f.bodies[key]; ok {
		return b, nil
	}
	return "", os.ErrNotExist
}

func (f *fakeRemote) Push(item Item, body string) error {
	f.addRemote(item.Kind, item.Name, body)
	return nil
}

// seedLocalAgent writes a local agent file under a temp ~/.claude and returns its
// content (so the test can predict its hash).
func seedLocalAgent(t *testing.T, name, body string) {
	t.Helper()
	home := os.Getenv("HOME")
	dir := filepath.Join(home, ".claude", "agents")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name+".md"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestComputeDriftCountsRemote proves the drift meter goes non-zero when HQ has
// items the laptop lacks and vice-versa — the wiring that was missing (U19).
func TestComputeDriftCountsRemote(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	seedLocalAgent(t, "local-only", "# local only\nbody")

	rem := newFakeRemote()
	rem.addRemote(KindSkill, "hq-only", "pull me down")

	report, err := ComputeDrift(rem)
	if err != nil {
		t.Fatalf("ComputeDrift: %v", err)
	}
	if report.DriftCount() != 2 {
		t.Fatalf("drift count = %d, want 2 (1 push + 1 pull); report=%+v", report.DriftCount(), report)
	}
	if report.InSync() {
		t.Error("populated remote with divergence should not be in sync")
	}
}

// TestReconcileConvergesIdempotent proves a reconcile pulls HQ-only items, pushes
// local-only items, and that re-running reaches (and stays at) in-sync.
func TestReconcileConvergesIdempotent(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	seedLocalAgent(t, "local-only", "# local only\nbody")

	rem := newFakeRemote()
	rem.addRemote(KindSkill, "hq-only", "pull me down")

	local, _ := ReadLocal()
	report := Diff(local, mustFetch(t, rem))
	pulled, pushed, errs := Reconcile(report, rem, local)
	if pulled != 1 || pushed != 1 || len(errs) != 0 {
		t.Fatalf("reconcile = pulled %d pushed %d errs %v, want 1/1/none", pulled, pushed, errs)
	}

	// After reconcile, the sets converge.
	after, err := ComputeDrift(rem)
	if err != nil {
		t.Fatalf("ComputeDrift after: %v", err)
	}
	if !after.InSync() {
		t.Fatalf("after reconcile should be in sync, got %+v", after)
	}

	// Running reconcile again on the converged set actuates nothing.
	local2, _ := ReadLocal()
	report2 := Diff(local2, mustFetch(t, rem))
	p2, pu2, e2 := Reconcile(report2, rem, local2)
	if p2 != 0 || pu2 != 0 || len(e2) != 0 {
		t.Fatalf("second reconcile should be a no-op, got pulled %d pushed %d errs %v", p2, pu2, e2)
	}
}

// TestReconcileBodyErrorNonFatal proves a remote item whose Body() fails is
// reported but does not abort the run — other items still reconcile.
func TestReconcileBodyErrorNonFatal(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	rem := newFakeRemote()
	rem.addRemote(KindSkill, "good", "ok")
	rem.addRemote(KindSkill, "bad", "boom")
	rem.bodyErr = string(KindSkill) + "/bad"

	local, _ := ReadLocal()
	report := Diff(local, mustFetch(t, rem))
	pulled, _, errs := Reconcile(report, rem, local)
	if pulled != 1 {
		t.Fatalf("the good item should still pull, pulled=%d", pulled)
	}
	if len(errs) != 1 {
		t.Fatalf("the bad item should report exactly one error, got %v", errs)
	}
}

func mustFetch(t *testing.T, src RemoteSource) []RemoteItem {
	t.Helper()
	r, err := src.Fetch()
	if err != nil {
		t.Fatalf("fetch: %v", err)
	}
	return r
}
