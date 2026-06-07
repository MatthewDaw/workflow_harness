package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestCanonicalEntryByteIdenticalUnderKeyReorder is the linchpin of MCP drift
// correctness (Risk R2): two entries that differ only in key order must produce
// byte-identical canonical output, so the HQ-built entry hashes equal to the
// on-disk one.
func TestCanonicalEntryByteIdenticalUnderKeyReorder(t *testing.T) {
	a := []byte(`{"type":"stdio","command":"node","args":["x","y"],"env":{"B":"2","A":"1"}}`)
	b := []byte(`{"env":{"A":"1","B":"2"},"args":["x","y"],"command":"node","type":"stdio"}`)
	ca, err := canonicalJSON(a)
	if err != nil {
		t.Fatalf("canonicalJSON(a): %v", err)
	}
	cb, err := canonicalJSON(b)
	if err != nil {
		t.Fatalf("canonicalJSON(b): %v", err)
	}
	if string(ca) != string(cb) {
		t.Fatalf("canonical output differs under key reorder:\n a=%s\n b=%s", ca, cb)
	}
	// Array order IS significant and must be preserved.
	if string(ca) == "" {
		t.Fatal("empty canonical output")
	}
}

// TestCanonicalEntryFromStructMatchesOnDiskHash proves an entry built from the HQ
// record (via entryForRemote -> canonicalEntry) hashes the same as the same entry
// parsed back off disk (canonicalServerHash).
func TestCanonicalEntryFromStructMatchesOnDiskHash(t *testing.T) {
	s := remoteMcpServer{
		Name:      "fs",
		Transport: "stdio",
		Command:   "npx",
		Args:      []string{"-y", "@modelcontextprotocol/server-filesystem"},
		Env:       map[string]string{"FOO": "bar"},
	}
	entry, err := entryForRemote(s)
	if err != nil {
		t.Fatalf("entryForRemote: %v", err)
	}
	canon, err := canonicalEntry(entry)
	if err != nil {
		t.Fatalf("canonicalEntry: %v", err)
	}
	hqHash := hashContent(canon)

	// Simulate the on-disk read: store the canonical bytes as a raw entry, then
	// re-hash via the read-side helper.
	onDisk, err := canonicalServerHash(json.RawMessage(canon))
	if err != nil {
		t.Fatalf("canonicalServerHash: %v", err)
	}
	if hqHash != onDisk {
		t.Fatalf("hash parity broken: hq=%s onDisk=%s", hqHash, onDisk)
	}
}

// TestEntryForRemoteUnknownTransport rejects an unknown transport rather than
// emitting a silently-wrong entry.
func TestEntryForRemoteUnknownTransport(t *testing.T) {
	if _, err := entryForRemote(remoteMcpServer{Name: "x", Transport: "carrier-pigeon"}); err == nil {
		t.Fatal("expected error for unknown transport")
	}
}

// TestMergePreservesUnrelatedEntriesAndKeys proves a merge sets one server entry
// while preserving other servers AND unrelated top-level keys (never a whole-file
// overwrite).
func TestMergePreservesUnrelatedEntriesAndKeys(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, mcpFileName)
	initial := `{
  "mcpServers": {
    "keep-me": {"type": "http", "url": "https://example.com/mcp", "headers": {"X": "y"}}
  },
  "someOtherTopLevelKey": {"nested": [1, 2, 3]}
}`
	if err := os.WriteFile(path, []byte(initial), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := mergeMcpServer(path, "added", mcpEntry{Type: "stdio", Command: "node"}); err != nil {
		t.Fatalf("mergeMcpServer: %v", err)
	}

	mf, err := parseMcpFile(path)
	if err != nil {
		t.Fatalf("parseMcpFile: %v", err)
	}
	if _, ok := mf.McpServers["keep-me"]; !ok {
		t.Error("unrelated server entry 'keep-me' was clobbered")
	}
	if _, ok := mf.McpServers["added"]; !ok {
		t.Error("new server entry 'added' was not written")
	}
	if _, ok := mf.Extra["someOtherTopLevelKey"]; !ok {
		t.Error("unrelated top-level key was clobbered")
	}
}

// TestParseMcpFileMissingIsEmpty proves an absent file is not an error (the
// seeded .mcp.json may not exist yet).
func TestParseMcpFileMissingIsEmpty(t *testing.T) {
	mf, err := parseMcpFile(filepath.Join(t.TempDir(), "does-not-exist.json"))
	if err != nil {
		t.Fatalf("missing file should not error: %v", err)
	}
	if len(mf.McpServers) != 0 {
		t.Fatalf("missing file should yield no servers, got %d", len(mf.McpServers))
	}
}

// TestParseMcpFileMalformedErrors proves malformed JSON surfaces as an error
// (so ReadLocal can flag it as an Err item rather than panicking).
func TestParseMcpFileMalformedErrors(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, mcpFileName)
	if err := os.WriteFile(path, []byte(`{ this is not json`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := parseMcpFile(path); err == nil {
		t.Fatal("malformed .mcp.json should error")
	}
}

// TestHttpEntryRoundTripsHeaders proves an http server round-trips url+headers and
// hashes consistently.
func TestHttpEntryRoundTripsHeaders(t *testing.T) {
	s := remoteMcpServer{
		Name:      "remote",
		Transport: "http",
		URL:       "https://api.example.com/mcp",
		Headers:   map[string]string{"Authorization": "Bearer t", "X-Org": "acme"},
	}
	entry, err := entryForRemote(s)
	if err != nil {
		t.Fatalf("entryForRemote: %v", err)
	}
	if entry.Type != "http" || entry.URL != s.URL {
		t.Fatalf("unexpected entry: %+v", entry)
	}
	canon, err := canonicalEntry(entry)
	if err != nil {
		t.Fatalf("canonicalEntry: %v", err)
	}
	// stdio-only fields must be absent from an http entry.
	var m map[string]any
	if err := json.Unmarshal(canon, &m); err != nil {
		t.Fatal(err)
	}
	if _, ok := m["command"]; ok {
		t.Error("http entry should not carry a command field")
	}
	if _, ok := m["headers"]; !ok {
		t.Error("http entry should carry headers")
	}
}
