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

// mcpRemoteItem builds the RemoteItem + canonical body for an MCP server exactly
// as remote.Fetch would, so a test can drive the same round-trip the daemon does.
func mcpRemoteItem(t *testing.T, s remoteMcpServer) (RemoteItem, string) {
	t.Helper()
	entry, err := entryForRemote(s)
	if err != nil {
		t.Fatalf("entryForRemote: %v", err)
	}
	canon, err := canonicalEntry(entry)
	if err != nil {
		t.Fatalf("canonicalEntry: %v", err)
	}
	return RemoteItem{Kind: KindMcp, Name: s.Name, Scope: "org", Hash: hashContent(canon)}, string(canon)
}

// TestMcpRoundTripInSync is the characterization test (U8 execution note): an HQ
// record -> canonical entry -> ApplyPulled into an empty .mcp.json -> ReadLocal
// -> Diff must report in_sync (hash parity). Both stdio and http are exercised.
func TestMcpRoundTripInSync(t *testing.T) {
	cases := []remoteMcpServer{
		{Name: "fs", Transport: "stdio", Command: "npx", Args: []string{"-y", "server-fs"}, Env: map[string]string{"K": "v"}},
		{Name: "remote", Transport: "http", URL: "https://api.example.com/mcp", Headers: map[string]string{"Authorization": "Bearer t"}},
	}
	for _, s := range cases {
		t.Run(s.Name, func(t *testing.T) {
			home := t.TempDir()
			t.Setenv("HOME", home)
			t.Setenv("USERPROFILE", home)

			ri, body := mcpRemoteItem(t, s)
			if err := ApplyPulled(ri, body); err != nil {
				t.Fatalf("ApplyPulled: %v", err)
			}
			local, err := ReadLocal()
			if err != nil {
				t.Fatalf("ReadLocal: %v", err)
			}
			report := Diff(local, []RemoteItem{ri})
			if !report.InSync() {
				t.Fatalf("round-trip should be in sync, got %+v (rows=%+v)", report, report.Rows)
			}
		})
	}
}

// TestMcpApplyPulledMergesPreservingUnrelated proves ApplyPulled into a .mcp.json
// that already holds an unrelated server and an unrelated top-level key preserves
// both (merge, not overwrite).
func TestMcpApplyPulledMergesPreservingUnrelated(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	plus, err := plusDir()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(plus, 0o755); err != nil {
		t.Fatal(err)
	}
	initial := `{"mcpServers":{"existing":{"type":"stdio","command":"old"}},"topKey":42}`
	if err := os.WriteFile(filepath.Join(plus, mcpFileName), []byte(initial), 0o644); err != nil {
		t.Fatal(err)
	}

	ri, body := mcpRemoteItem(t, remoteMcpServer{Name: "added", Transport: "stdio", Command: "new"})
	if err := ApplyPulled(ri, body); err != nil {
		t.Fatalf("ApplyPulled: %v", err)
	}

	mf, err := parseMcpFile(filepath.Join(plus, mcpFileName))
	if err != nil {
		t.Fatalf("parseMcpFile: %v", err)
	}
	if _, ok := mf.McpServers["existing"]; !ok {
		t.Error("unrelated server 'existing' was lost on merge")
	}
	if _, ok := mf.McpServers["added"]; !ok {
		t.Error("pulled server 'added' was not written")
	}
	if _, ok := mf.Extra["topKey"]; !ok {
		t.Error("unrelated top-level key 'topKey' was lost on merge")
	}
}

// TestMcpDriftClassifies proves a locally edited entry hashes as differs, an
// HQ-only server is needs_pull, and a local-only one is needs_push.
func TestMcpDriftClassifies(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	plus, _ := plusDir()
	if err := os.MkdirAll(plus, 0o755); err != nil {
		t.Fatal(err)
	}

	// On disk: "drifty" (will differ from HQ) and "local-only" (needs push).
	onDisk := `{"mcpServers":{
		"drifty":{"type":"stdio","command":"local-version"},
		"local-only":{"type":"stdio","command":"x"}
	}}`
	if err := os.WriteFile(filepath.Join(plus, mcpFileName), []byte(onDisk), 0o644); err != nil {
		t.Fatal(err)
	}

	driftyRemote, _ := mcpRemoteItem(t, remoteMcpServer{Name: "drifty", Transport: "stdio", Command: "hq-version"})
	hqOnly, _ := mcpRemoteItem(t, remoteMcpServer{Name: "hq-only", Transport: "stdio", Command: "z"})

	local, err := ReadLocal()
	if err != nil {
		t.Fatalf("ReadLocal: %v", err)
	}
	report := Diff(local, []RemoteItem{driftyRemote, hqOnly})
	if report.Differs != 1 {
		t.Errorf("want 1 differ, got %d (%+v)", report.Differs, report.Rows)
	}
	if report.NeedsPull != 1 {
		t.Errorf("want 1 needs_pull, got %d", report.NeedsPull)
	}
	if report.NeedsPush != 1 {
		t.Errorf("want 1 needs_push, got %d", report.NeedsPush)
	}
}

// TestMcpReconcilePullIdempotent proves a needs_pull MCP server is written by
// Reconcile and a second run is a no-op (idempotent), via a fake remote.
func TestMcpReconcilePullIdempotent(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	ri, body := mcpRemoteItem(t, remoteMcpServer{Name: "fs", Transport: "stdio", Command: "npx", Args: []string{"-y", "s"}})
	rem := newFakeRemote()
	rem.items = append(rem.items, ri)
	rem.bodies[string(KindMcp)+"/fs"] = body

	local, _ := ReadLocal()
	report := Diff(local, mustFetch(t, rem))
	pulled, _, errs := Reconcile(report, rem, local)
	if pulled != 1 || len(errs) != 0 {
		t.Fatalf("reconcile pull = %d errs %v, want 1/none", pulled, errs)
	}

	after, err := ComputeDrift(rem)
	if err != nil {
		t.Fatalf("ComputeDrift: %v", err)
	}
	if !after.InSync() {
		t.Fatalf("after pull should be in sync, got %+v", after)
	}

	// Second reconcile actuates nothing.
	local2, _ := ReadLocal()
	report2 := Diff(local2, mustFetch(t, rem))
	p2, _, e2 := Reconcile(report2, rem, local2)
	if p2 != 0 || len(e2) != 0 {
		t.Fatalf("second reconcile should be a no-op, got pulled %d errs %v", p2, e2)
	}
}

// TestReadLocalMcpMalformedNonFatal proves a malformed .mcp.json surfaces as a
// single Err item (not a panic) and does NOT blank the agent/skill items read
// alongside it.
func TestReadLocalMcpMalformedNonFatal(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	// A good agent alongside a broken .mcp.json.
	seedLocalAgent(t, "builder", "# builder\nbody")
	if err := os.WriteFile(filepath.Join(home, ".claude", mcpFileName), []byte("{ broken"), 0o644); err != nil {
		t.Fatal(err)
	}

	items, err := ReadLocal()
	if err != nil {
		t.Fatalf("ReadLocal should not error on malformed .mcp.json: %v", err)
	}
	var sawAgent, sawMcpErr bool
	for _, it := range items {
		if it.Kind == KindAgent && it.Name == "builder" && it.Err == "" {
			sawAgent = true
		}
		if it.Kind == KindMcp && it.Err != "" {
			sawMcpErr = true
		}
	}
	if !sawAgent {
		t.Error("the good agent must survive a malformed .mcp.json")
	}
	if !sawMcpErr {
		t.Error("malformed .mcp.json should surface as an Err item")
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
