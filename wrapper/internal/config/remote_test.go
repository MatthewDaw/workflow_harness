package config

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// mcpHQ is a minimal in-memory HQ for the wrapper's MCP REST contract: it serves
// GET /projects/{id} (with enabledMcpServers), GET /agents, GET /skills, GET
// /mcp-servers, and records POST /mcp-servers authoring payloads.
type mcpHQ struct {
	enabled    []string
	servers    []map[string]any
	lastPosted map[string]any
}

func (m *mcpHQ) server(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/agents", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"agents": []any{}})
	})
	mux.HandleFunc("/skills", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"skills": []any{}})
	})
	mux.HandleFunc("/mcp-servers", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			b, _ := io.ReadAll(r.Body)
			m.lastPosted = map[string]any{}
			_ = json.Unmarshal(b, &m.lastPosted)
			w.WriteHeader(http.StatusCreated)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"mcpServers": m.servers})
	})
	mux.HandleFunc("/projects/", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"enabledMcpServers": m.enabled})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

// TestFetchIntersectsEnabledMcp proves Fetch returns only catalog servers the
// project has opted into, with the canonical hash.
func TestFetchIntersectsEnabledMcp(t *testing.T) {
	hq := &mcpHQ{
		enabled: []string{"fs"},
		servers: []map[string]any{
			{"name": "fs", "scope": map[string]string{"tier": "org", "id": "acme"}, "transport": "stdio", "command": "npx", "args": []string{"-y", "s"}},
			{"name": "not-enabled", "scope": map[string]string{"tier": "org", "id": "acme"}, "transport": "http", "url": "https://x.example.com"},
		},
	}
	srv := hq.server(t)
	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")

	items, err := src.Fetch()
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	var mcp []RemoteItem
	for _, it := range items {
		if it.Kind == KindMcp {
			mcp = append(mcp, it)
		}
	}
	if len(mcp) != 1 || mcp[0].Name != "fs" {
		t.Fatalf("want only enabled 'fs', got %+v", mcp)
	}

	// The cached body must equal the canonical entry, so a pull reads back in-sync.
	body, err := src.Body(mcp[0])
	if err != nil {
		t.Fatalf("Body: %v", err)
	}
	wantEntry, _ := entryForRemote(remoteMcpServer{Name: "fs", Transport: "stdio", Command: "npx", Args: []string{"-y", "s"}})
	wantCanon, _ := canonicalEntry(wantEntry)
	if body != string(wantCanon) {
		t.Fatalf("cached body mismatch:\n got=%s\nwant=%s", body, wantCanon)
	}
	if mcp[0].Hash != hashContent(wantCanon) {
		t.Fatalf("hash mismatch: got %s want %s", mcp[0].Hash, hashContent(wantCanon))
	}
}

// TestPushMcpSerializesSingleEntry proves Push for a local-only MCP server reads
// the WHOLE .mcp.json (as Reconcile hands it) but authors only the one named
// entry to /mcp-servers as a structured payload (Risk R2).
func TestPushMcpSerializesSingleEntry(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	hq := &mcpHQ{}
	srv := hq.server(t)
	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")
	// Learn the org id (so the push scope is org#acme) by fetching once with a
	// catalog that carries an org-scoped record.
	hq.servers = []map[string]any{{"name": "seed", "scope": map[string]string{"tier": "org", "id": "acme"}, "transport": "stdio", "command": "x"}}
	if _, err := src.Fetch(); err != nil {
		t.Fatalf("Fetch (learn org): %v", err)
	}

	// A .mcp.json with TWO servers; Reconcile would hand Push the whole file.
	plus, _ := plusDir()
	if err := os.MkdirAll(plus, 0o755); err != nil {
		t.Fatal(err)
	}
	fileBody := `{"mcpServers":{
		"keep":{"type":"http","url":"https://other.example.com","headers":{"A":"b"}},
		"mine":{"type":"stdio","command":"node","args":["app.js"],"env":{"E":"1"}}
	}}`
	path := filepath.Join(plus, mcpFileName)
	if err := os.WriteFile(path, []byte(fileBody), 0o644); err != nil {
		t.Fatal(err)
	}

	item := Item{Kind: KindMcp, Name: "mine", Path: path}
	if err := src.Push(item, fileBody); err != nil {
		t.Fatalf("Push: %v", err)
	}

	if hq.lastPosted["name"] != "mine" {
		t.Fatalf("posted wrong server: %+v", hq.lastPosted)
	}
	if hq.lastPosted["transport"] != "stdio" {
		t.Fatalf("posted wrong transport: %+v", hq.lastPosted)
	}
	if hq.lastPosted["command"] != "node" {
		t.Fatalf("posted wrong command: %+v", hq.lastPosted)
	}
	// The other server must NOT have leaked into the payload (no whole-file push).
	if strings.Contains(toJSON(t, hq.lastPosted), "other.example.com") {
		t.Fatalf("whole .mcp.json leaked into the push payload: %+v", hq.lastPosted)
	}
	sc, ok := hq.lastPosted["scope"].(map[string]any)
	if !ok || sc["id"] != "acme" {
		t.Fatalf("push scope should be org#acme, got %+v", hq.lastPosted["scope"])
	}
}

// agentHQ is a minimal in-memory HQ for the agent REST contract: it serves
// GET /projects/{id} (enabledAgents), GET /agents (with structured fields), and
// empty /skills + /mcp-servers, and records the last POST /agents payload.
type agentHQ struct {
	enabled    []string
	agents     []map[string]any
	lastPosted map[string]any
}

func (a *agentHQ) server(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/agents", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			b, _ := io.ReadAll(r.Body)
			a.lastPosted = map[string]any{}
			_ = json.Unmarshal(b, &a.lastPosted)
			w.WriteHeader(http.StatusCreated)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"agents": a.agents})
	})
	mux.HandleFunc("/skills", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"skills": []any{}})
	})
	mux.HandleFunc("/mcp-servers", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"mcpServers": []any{}})
	})
	mux.HandleFunc("/projects/", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"enabledAgents": a.enabled})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

// TestRenderAgentFileOmission proves renderAgentFile emits frontmatter with the
// tools/model lines present only when set, and exactly one trailing newline.
func TestRenderAgentFileOmission(t *testing.T) {
	full := renderAgentFile(remoteAgent{
		Name: "rev", Description: "code reviewer",
		Tools: []string{"Read", "Grep"}, Model: "opus", Prompt: "Do the thing.",
	})
	want := "---\nname: rev\ndescription: code reviewer\ntools: Read, Grep\nmodel: opus\n---\nDo the thing.\n"
	if full != want {
		t.Fatalf("full render mismatch:\n got=%q\nwant=%q", full, want)
	}

	// Empty tools -> no tools line; empty model -> no model line. "inherit" is a
	// real value and must still be emitted (covered by the full case uses opus;
	// here we assert the empty-model omission specifically).
	min := renderAgentFile(remoteAgent{Name: "bare", Description: "", Prompt: "Hi"})
	wantMin := "---\nname: bare\ndescription: \n---\nHi\n"
	if min != wantMin {
		t.Fatalf("minimal render mismatch:\n got=%q\nwant=%q", min, wantMin)
	}
	if strings.Contains(min, "tools:") || strings.Contains(min, "model:") {
		t.Fatalf("empty tools/model must be omitted, got %q", min)
	}

	// "inherit" model is emitted (it is a value, not the empty sentinel).
	inh := renderAgentFile(remoteAgent{Name: "x", Prompt: "p", Model: "inherit"})
	if !strings.Contains(inh, "model: inherit\n") {
		t.Fatalf(`model "inherit" must be emitted, got %q`, inh)
	}

	// Prompt already ending in newlines collapses to exactly one trailing newline.
	multi := renderAgentFile(remoteAgent{Name: "x", Prompt: "p\n\n\n"})
	if !strings.HasSuffix(multi, "p\n") || strings.HasSuffix(multi, "p\n\n") {
		t.Fatalf("want exactly one trailing newline, got %q", multi)
	}
}

// TestFetchMaterializesAgentFrontmatterWithHashParity proves Fetch renders the
// full subagent file as the cached body AND hashes that same string — so a
// freshly pulled agent reads back in-sync (re-hashing the materialized file
// equals the RemoteItem hash).
func TestFetchMaterializesAgentFrontmatterWithHashParity(t *testing.T) {
	hq := &agentHQ{
		enabled: []string{"rev"},
		agents: []map[string]any{{
			"name":        "rev",
			"scope":       map[string]string{"tier": "org", "id": "acme"},
			"description": "code reviewer",
			"tools":       []string{"Read", "Grep"},
			"model":       "opus",
			"prompt":      "Review the diff.",
		}},
	}
	srv := hq.server(t)
	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")

	items, err := src.Fetch()
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	var ag *RemoteItem
	for i := range items {
		if items[i].Kind == KindAgent {
			ag = &items[i]
		}
	}
	if ag == nil || ag.Name != "rev" {
		t.Fatalf("want enabled agent 'rev', got %+v", items)
	}

	body, err := src.Body(*ag)
	if err != nil {
		t.Fatalf("Body: %v", err)
	}
	want := "---\nname: rev\ndescription: code reviewer\ntools: Read, Grep\nmodel: opus\n---\nReview the diff.\n"
	if body != want {
		t.Fatalf("materialized body mismatch:\n got=%q\nwant=%q", body, want)
	}
	// HASH PARITY: re-hashing the materialized file equals the RemoteItem hash.
	if ag.Hash != hashContent([]byte(body)) {
		t.Fatalf("hash parity broken: item=%s file=%s", ag.Hash, hashContent([]byte(body)))
	}
}

// TestPushAgentParsesFrontmatterNoDoubleWrap proves Push for a local agent file
// (which now carries frontmatter) sends prompt = body WITHOUT frontmatter plus
// the structured fields — so materialize -> push -> materialize is stable.
func TestPushAgentParsesFrontmatterNoDoubleWrap(t *testing.T) {
	hq := &agentHQ{
		agents: []map[string]any{{"name": "seed", "scope": map[string]string{"tier": "org", "id": "acme"}, "prompt": "x"}},
	}
	srv := hq.server(t)
	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")
	if _, err := src.Fetch(); err != nil { // learn org id
		t.Fatalf("Fetch (learn org): %v", err)
	}

	// The local file is exactly what renderAgentFile would have written.
	local := renderAgentFile(remoteAgent{
		Name: "rev", Description: "code reviewer",
		Tools: []string{"Read", "Grep"}, Model: "opus", Prompt: "Review the diff.",
	})
	if err := src.Push(Item{Kind: KindAgent, Name: "rev"}, local); err != nil {
		t.Fatalf("Push: %v", err)
	}

	// prompt must be the body WITHOUT frontmatter (no leading ---). A trailing
	// newline is tolerated because renderAgentFile TrimRights it on the way back
	// out (asserted by the round-trip check below).
	prompt, _ := hq.lastPosted["prompt"].(string)
	if strings.HasPrefix(prompt, "---") || strings.TrimSpace(prompt) != "Review the diff." {
		t.Fatalf("prompt should be frontmatter-stripped body, got %q", prompt)
	}
	if hq.lastPosted["description"] != "code reviewer" {
		t.Fatalf("description not parsed: %+v", hq.lastPosted)
	}
	if hq.lastPosted["model"] != "opus" {
		t.Fatalf("model not parsed: %+v", hq.lastPosted)
	}
	tools, _ := hq.lastPosted["tools"].([]any)
	if len(tools) != 2 || tools[0] != "Read" || tools[1] != "Grep" {
		t.Fatalf("tools not parsed: %+v", hq.lastPosted["tools"])
	}

	// ROUND-TRIP STABILITY: feeding the pushed fields back through renderAgentFile
	// reproduces the original local file byte-for-byte (no double frontmatter,
	// no field loss).
	rebuilt := renderAgentFile(remoteAgent{
		Name:        "rev",
		Description: hq.lastPosted["description"].(string),
		Model:       hq.lastPosted["model"].(string),
		Tools:       []string{tools[0].(string), tools[1].(string)},
		Prompt:      hq.lastPosted["prompt"].(string),
	})
	if rebuilt != local {
		t.Fatalf("round-trip not stable:\n got=%q\nwant=%q", rebuilt, local)
	}
}

// TestFetchThreadsAgentSkillDeps proves Fetch captures an enabled agent's skill
// dependencies (agentSchema.skills) so Reconcile can ensure them (U-Agent-Deps).
func TestFetchThreadsAgentSkillDeps(t *testing.T) {
	hq := &agentHQ{
		enabled: []string{"rev"},
		agents: []map[string]any{{
			"name":   "rev",
			"scope":  map[string]string{"tier": "org", "id": "acme"},
			"prompt": "Review.",
			"skills": []string{"lint", "format"},
		}},
	}
	srv := hq.server(t)
	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")
	if _, err := src.Fetch(); err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	deps := src.AgentSkills("rev")
	if len(deps) != 2 || deps[0] != "lint" || deps[1] != "format" {
		t.Fatalf("want agent deps [lint format], got %v", deps)
	}
	// An unknown agent yields nil.
	if src.AgentSkills("nope") != nil {
		t.Errorf("unknown agent should yield nil deps")
	}
}

// TestFetchExpandsAgentBundle proves an enabled AGENT BUNDLE expands into its
// member agents (which materialize) while the bundle record itself is never
// materialized, and each member agent's skills are brought into the effective
// skill set — the agent analog of skill-bundle expansion.
func TestFetchExpandsAgentBundle(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/agents", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"agents": []map[string]any{
			{
				"name":            "squad",
				"scope":           map[string]string{"tier": "org", "id": "acme"},
				"kind":            "bundle",
				"members":         []string{"rev"},
				"resolvedMembers": []string{"rev"},
			},
			{
				"name":   "rev",
				"scope":  map[string]string{"tier": "org", "id": "acme"},
				"prompt": "Review.",
				"skills": []string{"lint"},
			},
		}})
	})
	mux.HandleFunc("/skills", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"skills": []map[string]any{
			{"name": "lint", "scope": map[string]string{"tier": "org", "id": "acme"}, "kind": "skill", "body": "# Lint"},
		}})
	})
	mux.HandleFunc("/mcp-servers", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"mcpServers": []any{}})
	})
	mux.HandleFunc("/projects/", func(w http.ResponseWriter, _ *http.Request) {
		// Only the bundle is opted in — its member agent must be derived.
		_ = json.NewEncoder(w).Encode(map[string]any{"enabledAgentBundles": []string{"squad"}})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")
	items, err := src.Fetch()
	if err != nil {
		t.Fatalf("Fetch: %v", err)
	}

	var sawAgent, sawSkill bool
	for _, it := range items {
		if it.Kind == KindAgent && it.Name == "squad" {
			t.Fatalf("a bundle agent must NOT be materialized, got %+v", it)
		}
		if it.Kind == KindAgent && it.Name == "rev" {
			sawAgent = true
		}
		if it.Kind == KindSkill && it.Name == "lint" {
			sawSkill = true
		}
	}
	if !sawAgent {
		t.Errorf("member agent 'rev' should materialize from the enabled bundle, got %+v", items)
	}
	if !sawSkill {
		t.Errorf("member agent's skill 'lint' should be brought into the effective set, got %+v", items)
	}
	// The declared-agent set self-heals from the bundle's members.
	declared := src.DeclaredAgents()
	if len(declared) != 1 || declared[0] != "rev" {
		t.Errorf("want DeclaredAgents [rev], got %v", declared)
	}
}

func toJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}
