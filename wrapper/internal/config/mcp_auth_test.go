package config

import (
	"os"
	"path/filepath"
	"testing"
)

// writeClaudeJSON writes a .claude.json into root with the given mcpServers map
// (raw JSON object body) and returns the root.
func writeClaudeJSON(t *testing.T, root, mcpServersBody string) {
	t.Helper()
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"mcpServers":` + mcpServersBody + `}`
	if err := os.WriteFile(filepath.Join(root, mcpFileName), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestClassifyMcpServersOK proves a present server not in the needs-auth cache is ok.
func TestClassifyMcpServersOK(t *testing.T) {
	root := t.TempDir()
	writeClaudeJSON(t, root, `{"fs":{"type":"stdio","command":"npx"}}`)

	reports := ClassifyMcpServers(root, []string{"fs"})
	if len(reports) != 1 || reports[0].Status != McpAuthOK {
		t.Fatalf("want fs ok, got %+v", reports)
	}
	if reports[0].Command != "" {
		t.Errorf("ok server should carry no auth command, got %q", reports[0].Command)
	}
}

// TestClassifyMcpServersNeedsAuthArrayCache proves a present server listed in the
// needs-auth cache (array form) classifies as needs-auth with an interactive
// command — never auto-authed.
func TestClassifyMcpServersNeedsAuthArrayCache(t *testing.T) {
	root := t.TempDir()
	writeClaudeJSON(t, root, `{"github":{"type":"http","url":"https://api.github.com/mcp"}}`)
	if err := os.WriteFile(filepath.Join(root, mcpNeedsAuthCacheName), []byte(`["github"]`), 0o644); err != nil {
		t.Fatal(err)
	}

	reports := ClassifyMcpServers(root, []string{"github"})
	if len(reports) != 1 || reports[0].Status != McpAuthNeedsAuth {
		t.Fatalf("want github needs-auth, got %+v", reports)
	}
	if reports[0].Command == "" {
		t.Error("needs-auth server must surface an interactive command")
	}
}

// TestClassifyMcpServersNeedsAuthObjectCache proves the object-keyed cache shape
// is decoded too (Claude version drift tolerance).
func TestClassifyMcpServersNeedsAuthObjectCache(t *testing.T) {
	root := t.TempDir()
	writeClaudeJSON(t, root, `{"linear":{"type":"sse","url":"https://mcp.linear.app/sse"}}`)
	if err := os.WriteFile(filepath.Join(root, mcpNeedsAuthCacheName), []byte(`{"linear":{"reason":"oauth"}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	reports := ClassifyMcpServers(root, []string{"linear"})
	if len(reports) != 1 || reports[0].Status != McpAuthNeedsAuth {
		t.Fatalf("want linear needs-auth (object cache), got %+v", reports)
	}
}

// TestClassifyMcpServersFailedWhenAbsent proves a requested server NOT present in
// .claude.json mcpServers classifies as failed (the gate then fails loudly).
func TestClassifyMcpServersFailedWhenAbsent(t *testing.T) {
	root := t.TempDir()
	writeClaudeJSON(t, root, `{"other":{"type":"stdio","command":"x"}}`)

	reports := ClassifyMcpServers(root, []string{"missing"})
	if len(reports) != 1 || reports[0].Status != McpAuthFailed {
		t.Fatalf("want missing failed, got %+v", reports)
	}
}

// TestClassifyMcpServersFailedWhenNoFile proves that with no .claude.json at all,
// every requested server is failed (nothing materialized).
func TestClassifyMcpServersFailedWhenNoFile(t *testing.T) {
	root := t.TempDir()
	reports := ClassifyMcpServers(root, []string{"a", "b"})
	if len(reports) != 2 {
		t.Fatalf("want 2 reports, got %d", len(reports))
	}
	for _, r := range reports {
		if r.Status != McpAuthFailed {
			t.Errorf("server %s should be failed with no .claude.json, got %s", r.Name, r.Status)
		}
	}
}

// TestLoadMcpNeedsAuthAbsentIsClean proves an absent cache yields an empty,
// clean set (no server needs auth, no error).
func TestLoadMcpNeedsAuthAbsentIsClean(t *testing.T) {
	set, ok := loadMcpNeedsAuth(t.TempDir())
	if !ok {
		t.Error("absent cache should parse clean")
	}
	if len(set) != 0 {
		t.Errorf("absent cache should yield empty set, got %v", set)
	}
}

// TestLoadMcpNeedsAuthMalformed proves a malformed cache is non-fatal (empty set,
// ok=false) rather than crashing the gate.
func TestLoadMcpNeedsAuthMalformed(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, mcpNeedsAuthCacheName), []byte(`not json`), 0o644); err != nil {
		t.Fatal(err)
	}
	set, ok := loadMcpNeedsAuth(root)
	if ok {
		t.Error("malformed cache should report ok=false")
	}
	if len(set) != 0 {
		t.Errorf("malformed cache should yield empty set, got %v", set)
	}
}
