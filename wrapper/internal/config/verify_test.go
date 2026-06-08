package config

import (
	"os"
	"path/filepath"
	"testing"
)

// writeSkillFile writes a SKILL.md (with optional frontmatter name) for a skill.
func writeSkillFile(t *testing.T, plus, name, body string) {
	t.Helper()
	dir := filepath.Join(plus, "skills", name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, skillMainFile), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// writeAgentFile writes an agents/<name>.md for an agent.
func writeAgentFile(t *testing.T, plus, name, body string) {
	t.Helper()
	dir := filepath.Join(plus, "agents")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name+".md"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestVerifyAllPresentOK proves a fully materialized effective set passes the gate.
func TestVerifyAllPresentOK(t *testing.T) {
	plus := t.TempDir()
	writeSkillFile(t, plus, "lint", "---\nname: lint\n---\nLint things.\n")
	writeAgentFile(t, plus, "rev", "---\nname: rev\ndescription: x\n---\nReview.\n")

	rem := newFakeRemote()
	rem.items = []RemoteItem{
		{Kind: KindSkill, Name: "lint"},
		{Kind: KindAgent, Name: "rev"},
	}
	rem.depsByAg["rev"] = []string{"lint"}

	report := verifyAgainst(rem.items, rem, plus)
	if !report.OK() {
		t.Fatalf("fully materialized set should pass gate, got %+v", report.Items)
	}
	if report.Err() != nil {
		t.Fatalf("OK report should have nil Err, got %v", report.Err())
	}
}

// TestVerifyMissingSkillFails proves an enabled skill that never materialized fails
// the gate loudly.
func TestVerifyMissingSkillFails(t *testing.T) {
	plus := t.TempDir()
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindSkill, Name: "ghost"}}

	report := verifyAgainst(rem.items, rem, plus)
	if report.OK() {
		t.Fatal("a missing enabled skill must fail the gate")
	}
	if report.Err() == nil {
		t.Fatal("a partial install must return a non-nil error")
	}
}

// TestVerifySkillMissingFrontmatterFails proves a SKILL.md without a name field is
// invalid.
func TestVerifySkillMissingFrontmatterFails(t *testing.T) {
	plus := t.TempDir()
	writeSkillFile(t, plus, "noname", "Just a body, no frontmatter.\n")
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindSkill, Name: "noname"}}

	report := verifyAgainst(rem.items, rem, plus)
	if report.OK() {
		t.Fatal("a SKILL.md without name frontmatter must fail the gate")
	}
}

// TestVerifyAgentMissingDepSkillFails proves an agent whose dependency skill is not
// present fails the gate (even though the agent file itself is valid).
func TestVerifyAgentMissingDepSkillFails(t *testing.T) {
	plus := t.TempDir()
	writeAgentFile(t, plus, "rev", "---\nname: rev\n---\nReview.\n")
	// The dep skill "lint" is NOT materialized.
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindAgent, Name: "rev"}}
	rem.depsByAg["rev"] = []string{"lint"}

	report := verifyAgainst(rem.items, rem, plus)
	if report.OK() {
		t.Fatal("an agent missing a dependency skill must fail the gate")
	}
	var sawAgentInvalid bool
	for _, it := range report.Items {
		if it.Kind == KindAgent && it.Name == "rev" && it.Status == VerifyInvalid {
			sawAgentInvalid = true
		}
	}
	if !sawAgentInvalid {
		t.Fatalf("agent with missing dep should be invalid, got %+v", report.Items)
	}
}

// TestVerifyAgentBareNoFrontmatterFails proves a legacy bare-prompt agent (no
// frontmatter name) is flagged invalid by the gate.
func TestVerifyAgentBareNoFrontmatterFails(t *testing.T) {
	plus := t.TempDir()
	writeAgentFile(t, plus, "bare", "you are a bare agent with no frontmatter\n")
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindAgent, Name: "bare"}}

	report := verifyAgainst(rem.items, rem, plus)
	if report.OK() {
		t.Fatal("a bare agent with no frontmatter name must fail the gate")
	}
}

// TestVerifyMcpFailedFailsGate proves an enabled MCP server absent from
// .claude.json fails the gate.
func TestVerifyMcpFailedFailsGate(t *testing.T) {
	plus := t.TempDir()
	// No .claude.json => the enabled MCP server is failed.
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindMcp, Name: "fs"}}

	report := verifyAgainst(rem.items, rem, plus)
	if report.OK() {
		t.Fatal("a failed MCP server must fail the gate")
	}
}

// TestVerifyMcpNeedsAuthDoesNotFailGate proves a materialized-but-needs-auth MCP
// server passes the gate (it is actionable, not broken) and is surfaced.
func TestVerifyMcpNeedsAuthDoesNotFailGate(t *testing.T) {
	plus := t.TempDir()
	writeClaudeJSON(t, plus, `{"github":{"type":"http","url":"https://api.github.com/mcp"}}`)
	if err := os.WriteFile(filepath.Join(plus, mcpNeedsAuthCacheName), []byte(`["github"]`), 0o644); err != nil {
		t.Fatal(err)
	}
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindMcp, Name: "github"}}

	report := verifyAgainst(rem.items, rem, plus)
	if !report.OK() {
		t.Fatalf("a needs-auth MCP server must NOT fail the gate, got %+v", report.Items)
	}
	na := report.NeedsAuth()
	if len(na) != 1 || na[0].Name != "github" {
		t.Fatalf("needs-auth server should be surfaced, got %+v", na)
	}
	if na[0].Detail == "" {
		t.Error("needs-auth item should carry the interactive command")
	}
}

// TestVerifyMcpOKPasses proves a materialized, not-needs-auth MCP server passes.
func TestVerifyMcpOKPasses(t *testing.T) {
	plus := t.TempDir()
	writeClaudeJSON(t, plus, `{"fs":{"type":"stdio","command":"npx"}}`)
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindMcp, Name: "fs"}}

	report := verifyAgainst(rem.items, rem, plus)
	if !report.OK() {
		t.Fatalf("a materialized MCP server should pass, got %+v", report.Items)
	}
}
