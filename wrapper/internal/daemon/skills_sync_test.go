package daemon

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/config"
)

// fakeSource is an in-memory config.RemoteSource for the skills auto-sync tests.
// It records how often Fetch is called and serves one HQ-only skill so a
// reconcile has something to pull.
type fakeSource struct {
	mu      sync.Mutex
	fetches int32
	pulls   int32
}

func (f *fakeSource) Fetch() ([]config.RemoteItem, error) {
	atomic.AddInt32(&f.fetches, 1)
	body := "# hq-only skill\nbody"
	return []config.RemoteItem{
		{Kind: config.KindSkill, Name: "hq-only", Scope: "org", Hash: hashLike(body)},
	}, nil
}

func (f *fakeSource) Body(config.RemoteItem) (string, error) {
	atomic.AddInt32(&f.pulls, 1)
	return "# hq-only skill\nbody", nil
}

func (f *fakeSource) Push(config.Item, string) error { return nil }

func (f *fakeSource) AgentSkills(string) []string { return nil }

func (f *fakeSource) DeclaredSkills() []string { return nil }

func (f *fakeSource) DeclaredAgents() []string { return nil }

// hashLike reproduces config.hashContent for a body so the remote hash differs
// from "absent locally" (forcing a needs_pull). We don't need the exact hash —
// any stable non-empty value works because the local skill is missing.
func hashLike(s string) string { return "remote-" + s }

func newTestRuntime(t *testing.T, src config.RemoteSource) *Runtime {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatalf("New daemon: %v", err)
	}
	return &Runtime{
		d:              d,
		stop:           make(chan struct{}),
		cfgSrc:         src,
		syncedSessions: map[string]bool{},
	}
}

// TestReconcileSkillsPullsHQOnly proves the auto-sync pulls an HQ-only skill into
// the isolated claude+ tree (~/.claude+), the #1 user ask (applicable skills
// appear) without polluting the user's personal ~/.claude (U19/U21).
func TestReconcileSkillsPullsHQOnly(t *testing.T) {
	src := &fakeSource{}
	rt := newTestRuntime(t, src)

	rt.reconcileSkills()

	home := os.Getenv("HOME")
	// The pull lands in THIS repo's per-project root (~/.claude+/roots/<slug>),
	// resolved exactly as the runtime does.
	plus, err := config.ProjectConfigDir(rt.d.repoRoot)
	if err != nil {
		t.Fatalf("ProjectConfigDir: %v", err)
	}
	skill := filepath.Join(plus, "skills", "hq-only", "SKILL.md")
	b, err := os.ReadFile(skill)
	if err != nil {
		t.Fatalf("expected pulled skill at %s: %v", skill, err)
	}
	if string(b) != "# hq-only skill\nbody" {
		t.Fatalf("pulled skill body = %q", string(b))
	}
	// It must NOT land in the user's personal ~/.claude.
	if _, err := os.Stat(filepath.Join(home, ".claude", "skills", "hq-only")); !os.IsNotExist(err) {
		t.Fatalf("pulled skill must not pollute ~/.claude (err=%v)", err)
	}
}

// TestSyncSkillsOnceRunsOncePerSession proves the auto-sync fires once per NEW
// session id and that forgetSkillSync lets a reused id sync again (#1 + leak #11).
func TestSyncSkillsOnceRunsOncePerSession(t *testing.T) {
	src := &fakeSource{}
	rt := newTestRuntime(t, src)

	// First observation of "s1" marks it synced; repeat observations are no-ops.
	if !rt.markSyncedForTest("s1") {
		t.Fatal("first sync for s1 should proceed")
	}
	if rt.markSyncedForTest("s1") {
		t.Fatal("second sync for s1 should be skipped (once-per-session)")
	}

	// After the session ends, forgetSkillSync clears the marker so a reused id
	// re-syncs (and the map doesn't grow unbounded).
	rt.forgetSkillSync("s1")
	if !rt.markSyncedForTest("s1") {
		t.Fatal("after forget, s1 should sync again")
	}

	rt.syncedMu.Lock()
	_, present := rt.syncedSessions["gone"]
	rt.syncedMu.Unlock()
	if present {
		t.Fatal("never-seen session should not be present")
	}
}

// TestProjectOptInMaterializesOnlyEnabled proves the org-catalog + per-project
// opt-in contract end-to-end against a real HTTP source: HQ serves the whole org
// catalog (every item org-scoped, no ?project), GET /projects/{id} returns the
// project's enabledSkills/enabledAgents, and ONLY those opted-in items
// materialize into ~/.claude+. An org skill the project did NOT enable must not
// land locally.
func TestProjectOptInMaterializesOnlyEnabled(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	var sawProjectQuery bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("project") != "" {
			sawProjectQuery = true
		}
		w.Header().Set("content-type", "application/json")
		switch {
		case r.URL.Path == "/projects/myproj":
			// Project opts into one skill, one agent, and one MCP server (not the
			// whole catalog).
			_ = json.NewEncoder(w).Encode(map[string]any{
				"enabledSkills":     []string{"enabled-skill"},
				"enabledAgents":     []string{"enabled-agent"},
				"enabledMcpServers": []string{"enabled-mcp"},
			})
		case r.URL.Path == "/skills":
			// Whole org catalog, every item org-scoped.
			_ = json.NewEncoder(w).Encode(map[string]any{"skills": []map[string]any{
				{"name": "enabled-skill", "scope": map[string]string{"tier": "org", "id": "acme"}, "body": "# enabled\nbody"},
				{"name": "other-skill", "scope": map[string]string{"tier": "org", "id": "acme"}, "body": "# other\nbody"},
			}})
		case r.URL.Path == "/agents":
			_ = json.NewEncoder(w).Encode(map[string]any{"agents": []map[string]any{
				{"name": "enabled-agent", "scope": map[string]string{"tier": "org", "id": "acme"}, "prompt": "you are enabled"},
				{"name": "other-agent", "scope": map[string]string{"tier": "org", "id": "acme"}, "prompt": "you are other"},
			}})
		case r.URL.Path == "/mcp-servers":
			// Whole org MCP catalog; only "enabled-mcp" is opted into.
			_ = json.NewEncoder(w).Encode(map[string]any{"mcpServers": []map[string]any{
				{"name": "enabled-mcp", "scope": map[string]string{"tier": "org", "id": "acme"}, "transport": "stdio", "command": "echo", "args": []string{"hi"}},
				{"name": "other-mcp", "scope": map[string]string{"tier": "org", "id": "acme"}, "transport": "stdio", "command": "nope"},
			}})
		case r.URL.Path == "/workflows":
			_ = json.NewEncoder(w).Encode(map[string]any{"workflows": []any{}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	src := config.NewHTTPRemoteSource(srv.URL, "tok", "myproj")

	// The read/write/reconcile helpers take the per-project registry dir
	// explicitly; this test materializes into a fixed dir under the temp HOME and
	// asserts on it directly.
	plus := filepath.Join(home, ".claude+")
	report, err := config.ComputeDrift(src, plus)
	if err != nil {
		t.Fatalf("ComputeDrift: %v", err)
	}
	local, err := config.ReadLocal(plus)
	if err != nil {
		t.Fatalf("ReadLocal: %v", err)
	}
	pulled, _, errs := config.Reconcile(report, src, local, plus)
	if len(errs) != 0 {
		t.Fatalf("reconcile errs: %v", errs)
	}
	// Only the three opted-in items pull down (skill + agent + MCP server).
	if pulled != 3 {
		t.Fatalf("pulled = %d, want 3 (one enabled skill + one enabled agent + one enabled mcp)", pulled)
	}
	if sawProjectQuery {
		t.Fatal("catalog GETs must not carry a ?project query param (org-wide catalog)")
	}

	// Enabled items materialize.
	if _, err := os.Stat(filepath.Join(plus, "skills", "enabled-skill", "SKILL.md")); err != nil {
		t.Fatalf("enabled skill should materialize: %v", err)
	}
	if _, err := os.Stat(filepath.Join(plus, "agents", "enabled-agent.md")); err != nil {
		t.Fatalf("enabled agent should materialize: %v", err)
	}
	// An org item NOT in the project opt-in must NOT land locally.
	if _, err := os.Stat(filepath.Join(plus, "skills", "other-skill")); !os.IsNotExist(err) {
		t.Fatalf("non-opted-in org skill must not materialize (err=%v)", err)
	}
	if _, err := os.Stat(filepath.Join(plus, "agents", "other-agent.md")); !os.IsNotExist(err) {
		t.Fatalf("non-opted-in org agent must not materialize (err=%v)", err)
	}

	// The enabled MCP server merges into ~/.claude+/.claude.json mcpServers
	// (U-MCP-Target); the non-opted-in one must not appear. (MCP servers materialize
	// as entries in a shared file, not one-file-per-item — so assert on the parsed
	// document.) The enabled server's name must also land in enabledMcpjsonServers
	// so Claude actually launches it.
	mcpBytes, err := os.ReadFile(filepath.Join(plus, ".claude.json"))
	if err != nil {
		t.Fatalf("enabled mcp server should materialize .claude.json: %v", err)
	}
	var mcpDoc struct {
		McpServers            map[string]json.RawMessage `json:"mcpServers"`
		EnabledMcpjsonServers []string                   `json:"enabledMcpjsonServers"`
	}
	if err := json.Unmarshal(mcpBytes, &mcpDoc); err != nil {
		t.Fatalf("parse materialized .claude.json: %v", err)
	}
	if _, ok := mcpDoc.McpServers["enabled-mcp"]; !ok {
		t.Fatalf("enabled mcp server missing from .claude.json: %s", string(mcpBytes))
	}
	if _, ok := mcpDoc.McpServers["other-mcp"]; ok {
		t.Fatalf("non-opted-in mcp server must not materialize: %s", string(mcpBytes))
	}
	var sawEnabled bool
	for _, n := range mcpDoc.EnabledMcpjsonServers {
		if n == "enabled-mcp" {
			sawEnabled = true
		}
	}
	if !sawEnabled {
		t.Fatalf("enabled-mcp should be added to enabledMcpjsonServers: %s", string(mcpBytes))
	}
}

// TestEmptyOptInMaterializesNothing proves the explicit-opt-in rule: a project
// with empty enabledSkills/enabledAgents pulls NOTHING from a populated org
// catalog on first sync.
func TestEmptyOptInMaterializesNothing(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/projects/"):
			_ = json.NewEncoder(w).Encode(map[string]any{}) // no enabled arrays
		case r.URL.Path == "/skills":
			_ = json.NewEncoder(w).Encode(map[string]any{"skills": []map[string]any{
				{"name": "org-skill", "scope": map[string]string{"tier": "org", "id": "acme"}, "body": "# x"},
			}})
		case r.URL.Path == "/agents":
			_ = json.NewEncoder(w).Encode(map[string]any{"agents": []map[string]any{}})
		case r.URL.Path == "/mcp-servers":
			// Populated org MCP catalog; empty opt-in must still pull nothing.
			_ = json.NewEncoder(w).Encode(map[string]any{"mcpServers": []map[string]any{
				{"name": "org-mcp", "scope": map[string]string{"tier": "org", "id": "acme"}, "transport": "stdio", "command": "echo"},
			}})
		case r.URL.Path == "/workflows":
			// Populated org workflow catalog; empty opt-in must still pull nothing.
			_ = json.NewEncoder(w).Encode(map[string]any{"workflows": []map[string]any{
				{"name": "org-workflow", "scope": map[string]string{"tier": "org", "id": "acme"}, "kind": "workflow", "nodes": []map[string]any{{"id": "n", "agent": "org-agent"}}},
			}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	src := config.NewHTTPRemoteSource(srv.URL, "tok", "p1")
	plus := filepath.Join(home, ".claude+")
	report, err := config.ComputeDrift(src, plus)
	if err != nil {
		t.Fatalf("ComputeDrift: %v", err)
	}
	if report.NeedsPull != 0 {
		t.Fatalf("empty opt-in must pull nothing, got NeedsPull=%d", report.NeedsPull)
	}
}

// TestReconcileSkillsInjectsCandidateLearnings proves the daemon's per-session
// reconcile injects each MATERIALIZED skill's corroborated candidate-learnings
// block (U12) into its on-disk SKILL.md, and that the injected block does NOT make
// the skill drift (no re-pull churn). Drives the real HTTP source through
// reconcileSkills so the direct-enable materialize path is covered end-to-end.
func TestReconcileSkillsInjectsCandidateLearnings(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		switch {
		case r.URL.Path == "/skills/deploy/candidate-learnings":
			_ = json.NewEncoder(w).Encode(map[string]any{"learnings": []map[string]string{
				{"text": "Pin the image tag before deploy."},
			}})
		case strings.HasPrefix(r.URL.Path, "/projects/"):
			_ = json.NewEncoder(w).Encode(map[string]any{"enabledSkills": []string{"deploy"}})
		case r.URL.Path == "/skills":
			_ = json.NewEncoder(w).Encode(map[string]any{"skills": []map[string]any{
				{"name": "deploy", "scope": map[string]string{"tier": "org", "id": "acme"}, "body": "# Deploy\nRun the steps.\n"},
			}})
		case r.URL.Path == "/agents":
			_ = json.NewEncoder(w).Encode(map[string]any{"agents": []any{}})
		case r.URL.Path == "/mcp-servers":
			_ = json.NewEncoder(w).Encode(map[string]any{"mcpServers": []any{}})
		case r.URL.Path == "/workflows":
			_ = json.NewEncoder(w).Encode(map[string]any{"workflows": []any{}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	src := config.NewHTTPRemoteSource(srv.URL, "tok", "myproj")
	rt := newTestRuntime(t, src)

	rt.reconcileSkills()

	plus, err := config.ProjectConfigDir(rt.d.repoRoot)
	if err != nil {
		t.Fatalf("ProjectConfigDir: %v", err)
	}
	skillPath := filepath.Join(plus, "skills", "deploy", "SKILL.md")
	b, err := os.ReadFile(skillPath)
	if err != nil {
		t.Fatalf("expected materialized skill: %v", err)
	}
	if !strings.Contains(string(b), "Pin the image tag before deploy.") {
		t.Fatalf("candidate-learnings block not injected:\n%s", b)
	}

	// The injected block must not cause drift: a fresh ComputeDrift over the on-disk
	// tree vs HQ must report in-sync (otherwise the block would force a re-pull).
	report, err := config.ComputeDrift(src, plus)
	if err != nil {
		t.Fatalf("ComputeDrift: %v", err)
	}
	for _, row := range report.Rows {
		if row.Kind == config.KindSkill && row.Name == "deploy" && row.Drift != config.DriftInSync {
			t.Fatalf("injected block made the skill drift (%s) — would re-pull", row.Drift)
		}
	}
}

// markSyncedForTest mirrors the once-gate inside syncSkillsOnce without spawning
// the async reconcile, returning whether this call was the first for the id.
func (rt *Runtime) markSyncedForTest(sessID string) bool {
	rt.syncedMu.Lock()
	defer rt.syncedMu.Unlock()
	if rt.syncedSessions[sessID] {
		return false
	}
	rt.syncedSessions[sessID] = true
	return true
}
