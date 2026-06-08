package config

// MCP auth classification (U-MCP-Auth-Report). After an MCP server is materialized
// into <root>/.claude.json mcpServers, the wrapper still cannot claim the server is
// usable: an http/sse (or remote-OAuth) server may require an interactive login the
// daemon must NOT attempt headlessly. Claude Code records the servers it could not
// reach without auth in <root>/mcp-needs-auth-cache.json; we read that cache to
// classify every synced server so the verification gate (U-Verify-Gate) can fail a
// partial install loudly while distinguishing "needs interactive auth" (actionable,
// not a wrapper bug) from "failed" (missing entry / malformed config).
//
// We DO NOT attempt headless OAuth. A needs-auth server is reported with the exact
// interactive command to run; we never claim auto-auth.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
)

// McpAuthStatus classifies one synced MCP server's readiness.
type McpAuthStatus string

const (
	// McpAuthOK: the server entry is present in .claude.json and is NOT listed in
	// the needs-auth cache, so it is ready to launch.
	McpAuthOK McpAuthStatus = "ok"
	// McpAuthNeedsAuth: the server is listed in mcp-needs-auth-cache.json — it
	// requires an interactive login the wrapper must not perform headlessly.
	McpAuthNeedsAuth McpAuthStatus = "needs-auth"
	// McpAuthFailed: the server could not be materialized at all (absent from
	// .claude.json mcpServers after a sync, or its entry is malformed).
	McpAuthFailed McpAuthStatus = "failed"
)

// McpAuthReport is one synced server's classification plus, for needs-auth, the
// interactive command a human should run to complete the login.
type McpAuthReport struct {
	Name    string        `json:"name"`
	Status  McpAuthStatus `json:"status"`
	Command string        `json:"command,omitempty"` // interactive auth command (needs-auth only)
}

// mcpNeedsAuthCacheName is the file Claude Code maintains under a config root
// listing MCP servers whose auth handshake has not completed. Its exact on-disk
// shape varies across Claude versions, so loadMcpNeedsAuth decodes it permissively
// (a JSON array of names, or an object keyed by server name) into a name set.
const mcpNeedsAuthCacheName = "mcp-needs-auth-cache.json"

// loadMcpNeedsAuth reads <root>/mcp-needs-auth-cache.json and returns the set of
// server names that require interactive auth. An absent or empty cache yields an
// empty set (nothing needs auth) — never an error. The cache is decoded
// permissively to tolerate Claude version drift:
//   - a JSON array of strings:        ["github", "linear"]
//   - a JSON object keyed by name:    {"github": {...}, "linear": {...}}
//
// A malformed cache (neither shape) yields an empty set rather than failing the
// whole gate: a server simply will not be classified needs-auth, and the gate's
// own presence check still runs. (ok bit is true when the cache parsed cleanly.)
func loadMcpNeedsAuth(root string) (set map[string]bool, ok bool) {
	set = map[string]bool{}
	b, err := os.ReadFile(filepath.Join(root, mcpNeedsAuthCacheName))
	if err != nil {
		return set, true // absent cache == nothing needs auth (clean)
	}
	if len(bytes.TrimSpace(b)) == 0 {
		return set, true
	}
	// Try array-of-names first.
	var names []string
	if err := json.Unmarshal(b, &names); err == nil {
		for _, n := range names {
			if n != "" {
				set[n] = true
			}
		}
		return set, true
	}
	// Fall back to object-keyed-by-name.
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(b, &obj); err == nil {
		for n := range obj {
			if n != "" {
				set[n] = true
			}
		}
		return set, true
	}
	return set, false // malformed
}

// mcpAuthCommand returns the interactive command a human runs to complete an MCP
// server's OAuth handshake. Claude Code's own flow is `claude mcp auth <name>`;
// surfacing it (rather than attempting headless OAuth) keeps the wrapper honest
// about what it did and did not do.
func mcpAuthCommand(name string) string {
	return "claude mcp auth " + name
}

// ClassifyMcpServers classifies each of the given synced MCP server names against
// the on-disk state in `root`: a server present in .claude.json mcpServers and NOT
// in the needs-auth cache is ok; one present but listed in the cache is needs-auth
// (with the interactive command); one absent from mcpServers (or whose entry is
// malformed) is failed. Pure over (root, names) so the verification gate can call
// it after a sync and fail loudly on any non-ok server. Order of the returned
// reports follows the input name order for stable output.
func ClassifyMcpServers(root string, names []string) []McpAuthReport {
	needsAuth, _ := loadMcpNeedsAuth(root)

	// Read the materialized servers once. A malformed .claude.json means NO server
	// is present, so every requested name classifies as failed (the gate then fails
	// loudly, which is correct — nothing materialized).
	present := map[string]bool{}
	mf, err := parseMcpFile(filepath.Join(root, mcpFileName))
	if err == nil {
		for n, raw := range mf.McpServers {
			// A present-but-unparseable entry is treated as absent (failed) so a
			// corrupt entry never reports ok.
			if _, hErr := canonicalServerHash(raw); hErr == nil {
				present[n] = true
			}
		}
	}

	out := make([]McpAuthReport, 0, len(names))
	for _, name := range names {
		switch {
		case !present[name]:
			out = append(out, McpAuthReport{Name: name, Status: McpAuthFailed})
		case needsAuth[name]:
			out = append(out, McpAuthReport{Name: name, Status: McpAuthNeedsAuth, Command: mcpAuthCommand(name)})
		default:
			out = append(out, McpAuthReport{Name: name, Status: McpAuthOK})
		}
	}
	return out
}
