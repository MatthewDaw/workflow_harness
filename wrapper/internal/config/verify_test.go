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

// TestVerifyDeclaredBundleNameFails proves the guarantee behind /hq-sync: a skill
// the project DECLARES enabled but that never resolved to a materializable record
// (e.g. a bundle name mistakenly placed in enabledSkills, so Fetch skipped it and
// it is absent from the effective `remote` set) fails the gate loudly — it is not
// silently ignored just because it never appeared on disk. This is the exact gap
// that left registered/enabled gstack skills undiscoverable after a "successful"
// sync.
func TestVerifyDeclaredBundleNameFails(t *testing.T) {
	plus := t.TempDir()
	rem := newFakeRemote()
	// Effective set is empty (the bundle was skipped by Fetch), but the project
	// declared "gstack" enabled.
	rem.items = nil
	rem.declared = []string{"gstack"}

	report := verifyAgainst(rem.items, rem, plus)
	if report.OK() {
		t.Fatal("a declared-but-unmaterialized skill must fail the gate")
	}
	if report.Err() == nil {
		t.Fatal("a partial install must return a non-nil error")
	}
	var sawGstackMissing bool
	for _, it := range report.Items {
		if it.Kind == KindSkill && it.Name == "gstack" && it.Status == VerifyMissing {
			sawGstackMissing = true
			if it.Detail == "" {
				t.Error("a declared-but-missing skill should carry an actionable detail")
			}
		}
	}
	if !sawGstackMissing {
		t.Fatalf("declared skill gstack should be flagged missing, got %+v", report.Items)
	}
}

// TestVerifyDeclaredCoveredOnDiskPasses proves a declared skill that DID materialize
// (present on disk, e.g. pulled or repo-local) passes the gate and is not duplicated
// when it is also in the effective `remote` set.
func TestVerifyDeclaredCoveredOnDiskPasses(t *testing.T) {
	plus := t.TempDir()
	writeSkillFile(t, plus, "browse", "---\nname: browse\n---\nBrowse.\n")
	rem := newFakeRemote()
	rem.items = []RemoteItem{{Kind: KindSkill, Name: "browse"}}
	rem.declared = []string{"browse"} // both effective AND declared

	report := verifyAgainst(rem.items, rem, plus)
	if !report.OK() {
		t.Fatalf("a materialized declared skill should pass, got %+v", report.Items)
	}
	var count int
	for _, it := range report.Items {
		if it.Kind == KindSkill && it.Name == "browse" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("declared skill also in the effective set must not be double-reported, got %d items", count)
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
