package config

// Verification gate (U-Verify-Gate). A sync that reports "pulled N" is not proof
// the install is usable: a skill dir may be missing its SKILL.md or name
// frontmatter, an agent file may be malformed or reference skills that never
// landed, and an MCP server may have failed to materialize. VerifyEffectiveSet runs
// a final pass over the EFFECTIVE enabled set (what HQ says this project should
// have) against what is actually on disk, so SyncSkillsNow / cmdSyncSkills can fail
// LOUDLY (non-zero) on a partial install rather than silently leaving a session
// half-equipped.
//
// It classifies every enabled item ok / problem and returns a structured report;
// OK() is the gate the caller checks. A needs-auth MCP server is NOT a failure
// (the human just has to complete an interactive login) — only a FAILED server is.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// VerifyStatus classifies one enabled item's on-disk readiness.
type VerifyStatus string

const (
	VerifyOK        VerifyStatus = "ok"
	VerifyMissing   VerifyStatus = "missing"    // enabled but not materialized
	VerifyInvalid   VerifyStatus = "invalid"    // present but malformed/incomplete
	VerifyNeedsAuth VerifyStatus = "needs-auth" // MCP server present but needs interactive login
)

// VerifyItem is one enabled item's verification result.
type VerifyItem struct {
	Kind   Kind         `json:"kind"`
	Name   string       `json:"name"`
	Status VerifyStatus `json:"status"`
	Detail string       `json:"detail,omitempty"` // why it failed / the auth command
}

// VerifyReport is the full gate result over the effective set.
type VerifyReport struct {
	Items []VerifyItem `json:"items"`
}

// OK reports whether the install is fully usable: every enabled skill and agent is
// materialized and valid, every agent's skills are present, and no MCP server
// FAILED. A needs-auth MCP server does NOT fail the gate (it is actionable via an
// interactive login, not a broken sync).
func (r VerifyReport) OK() bool {
	for _, it := range r.Items {
		switch it.Status {
		case VerifyMissing, VerifyInvalid:
			return false
		}
	}
	return true
}

// Problems returns the items that fail the gate (missing/invalid), for a loud
// error message. needs-auth and ok are excluded.
func (r VerifyReport) Problems() []VerifyItem {
	var out []VerifyItem
	for _, it := range r.Items {
		if it.Status == VerifyMissing || it.Status == VerifyInvalid {
			out = append(out, it)
		}
	}
	return out
}

// NeedsAuth returns the MCP servers that materialized but require an interactive
// login. They do not fail the gate but the caller should surface them so the human
// knows to run the command.
func (r VerifyReport) NeedsAuth() []VerifyItem {
	var out []VerifyItem
	for _, it := range r.Items {
		if it.Status == VerifyNeedsAuth {
			out = append(out, it)
		}
	}
	return out
}

// Summary is a one-line human description of the gate result (for the CLI / skill).
func (r VerifyReport) Summary() string {
	ok, problems, needsAuth := 0, 0, 0
	for _, it := range r.Items {
		switch it.Status {
		case VerifyOK:
			ok++
		case VerifyNeedsAuth:
			needsAuth++
		default:
			problems++
		}
	}
	return fmt.Sprintf("%d ok, %d need-auth, %d problem(s)", ok, needsAuth, problems)
}

// Err returns a non-nil error naming every gate failure when the install is
// partial, else nil. SyncSkillsNow / cmdSyncSkills return this so a partial install
// exits non-zero (fail loudly).
func (r VerifyReport) Err() error {
	probs := r.Problems()
	if len(probs) == 0 {
		return nil
	}
	parts := make([]string, 0, len(probs))
	for _, p := range probs {
		parts = append(parts, fmt.Sprintf("%s %q: %s (%s)", p.Kind, p.Name, p.Status, p.Detail))
	}
	return fmt.Errorf("verification failed for %d enabled item(s): %s", len(probs), strings.Join(parts, "; "))
}

// VerifyEffectiveSet verifies the effective enabled set (from src.Fetch) against
// what is materialized under `plus`. For each:
//   - skill: a SKILL.md must exist with a `name:` frontmatter field;
//   - agent: an agents/<name>.md must exist with valid frontmatter (a name), and
//     every skill it depends on (src.AgentSkills) must be present as a skill dir;
//   - mcp: classified via ClassifyMcpServers — failed => invalid, needs-auth =>
//     needs-auth, ok => ok.
//
// It re-uses Fetch's effective set so the gate matches exactly what was synced. The
// returned report is sorted (kind, name) for stable output.
func VerifyEffectiveSet(src RemoteSource, plus string) (VerifyReport, error) {
	remote, err := src.Fetch()
	if err != nil {
		return VerifyReport{}, err
	}
	return verifyAgainst(remote, src, plus), nil
}

// verifyAgainst is the pure core of the gate (no network): it checks the given
// effective set against the on-disk `plus` tree. Split out so it is trivially
// testable with an in-memory effective set + a temp dir.
func verifyAgainst(remote []RemoteItem, src RemoteSource, plus string) VerifyReport {
	// Index materialized skills (dir form) so agent-dep checks are O(1).
	skillPresent := map[string]bool{}
	reportedSkills := map[string]bool{} // skill names already in the report (no dups)
	var mcpNames []string

	var report VerifyReport
	for _, ri := range remote {
		switch ri.Kind {
		case KindSkill:
			st, detail := verifySkill(plus, ri.Name)
			if st == VerifyOK {
				skillPresent[ri.Name] = true
			}
			reportedSkills[ri.Name] = true
			report.Items = append(report.Items, VerifyItem{Kind: KindSkill, Name: ri.Name, Status: st, Detail: detail})
		case KindWorkflow:
			st, detail := verifyWorkflow(plus, ri.Name)
			report.Items = append(report.Items, VerifyItem{Kind: KindWorkflow, Name: ri.Name, Status: st, Detail: detail})
		case KindMcp:
			mcpNames = append(mcpNames, ri.Name)
		}
	}

	// Declared-set coverage — the guarantee behind /hq-sync. The `remote` loop above
	// only sees the EFFECTIVE set: skills that resolved to a real catalog record AND
	// are enabled. A skill the project DECLARES enabled but that has no resolvable
	// record — a bundle name placed in enabledSkills, a dangling bundle member, or a
	// record absent for this project's org — never appears in `remote`, so without
	// this pass it would silently never land yet the gate would still pass. Assert
	// every declared name is materialized on disk; flag any that is not so the sync
	// fails loudly and names it. (A declared name already reported, or one present on
	// disk from the repo's own .claude, is not re-flagged.)
	for _, name := range src.DeclaredSkills() {
		if reportedSkills[name] {
			continue
		}
		reportedSkills[name] = true
		if st, _ := verifySkill(plus, name); st == VerifyOK {
			skillPresent[name] = true
			report.Items = append(report.Items, VerifyItem{Kind: KindSkill, Name: name, Status: VerifyOK})
			continue
		}
		report.Items = append(report.Items, VerifyItem{
			Kind:   KindSkill,
			Name:   name,
			Status: VerifyMissing,
			Detail: "enabled on this project but did not materialize — it has no skill record in this project's org catalog, or it names a bundle (enable the bundle via /bundles, or enable its leaf skills directly)",
		})
	}

	// Agents need the skill-present index complete, so verify them in a second pass.
	reportedAgents := map[string]bool{}
	for _, ri := range remote {
		if ri.Kind != KindAgent {
			continue
		}
		reportedAgents[ri.Name] = true
		st, detail := verifyAgent(plus, ri.Name, src.AgentSkills(ri.Name), skillPresent)
		report.Items = append(report.Items, VerifyItem{Kind: KindAgent, Name: ri.Name, Status: st, Detail: detail})
	}

	// Declared-agent coverage — the agent analog of the declared-skill pass above.
	// `remote` only carries the EFFECTIVE agent set (records that resolved AND are
	// enabled); an agent the project DECLARES enabled but that has no resolvable
	// record — a bundle name wrongly in enabledAgents, a dangling member, or a
	// record absent for this org — never appears, so without this pass it would
	// silently never land yet the gate would pass. Assert every declared agent
	// materialized; flag any that did not so the sync fails loudly and names it.
	for _, name := range src.DeclaredAgents() {
		if reportedAgents[name] {
			continue
		}
		reportedAgents[name] = true
		st, detail := verifyAgent(plus, name, src.AgentSkills(name), skillPresent)
		if st != VerifyOK {
			detail = "enabled on this project but did not materialize — it has no agent record in this project's org catalog, or it names a bundle (enable the bundle via /agent-bundles, or enable its member agents directly)"
		}
		report.Items = append(report.Items, VerifyItem{Kind: KindAgent, Name: name, Status: st, Detail: detail})
	}

	// MCP servers: classify all enabled names against the on-disk .claude.json +
	// needs-auth cache in one shot.
	for _, ar := range ClassifyMcpServers(plus, mcpNames) {
		st, detail := mcpVerifyStatus(ar)
		report.Items = append(report.Items, VerifyItem{Kind: KindMcp, Name: ar.Name, Status: st, Detail: detail})
	}

	sort.Slice(report.Items, func(i, j int) bool {
		if report.Items[i].Kind != report.Items[j].Kind {
			return report.Items[i].Kind < report.Items[j].Kind
		}
		return report.Items[i].Name < report.Items[j].Name
	})
	return report
}

// verifySkill checks that skills/<name>/SKILL.md exists and carries a `name:`
// frontmatter field (the minimum for Claude to register the skill).
func verifySkill(plus, name string) (VerifyStatus, string) {
	path := filepath.Join(plus, "skills", name, skillMainFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return VerifyMissing, "missing " + filepath.Join("skills", name, skillMainFile)
	}
	if len(strings.TrimSpace(string(b))) == 0 {
		return VerifyInvalid, "empty SKILL.md"
	}
	if frontmatterField(string(b), "name") == "" {
		return VerifyInvalid, "SKILL.md missing name frontmatter"
	}
	return VerifyOK, ""
}

// verifyWorkflow checks that workflows/<name>.json exists and parses as JSON (the
// minimum for the workflow spec to be usable). A workflow is structured data, so —
// unlike a skill (name frontmatter) or an agent (frontmatter name) — the gate's
// validity check is that the materialized body is well-formed JSON.
func verifyWorkflow(plus, name string) (VerifyStatus, string) {
	path := filepath.Join(plus, "workflows", name+".json")
	b, err := os.ReadFile(path)
	if err != nil {
		return VerifyMissing, "missing workflows/" + name + ".json"
	}
	if len(strings.TrimSpace(string(b))) == 0 {
		return VerifyInvalid, "empty workflow file"
	}
	if !json.Valid(b) {
		return VerifyInvalid, "workflow file is not valid JSON"
	}
	return VerifyOK, ""
}

// verifyAgent checks that agents/<name>.md exists with valid frontmatter (at least
// a name), and that every skill the agent depends on is present as a skill dir.
func verifyAgent(plus, name string, deps []string, skillPresent map[string]bool) (VerifyStatus, string) {
	path := filepath.Join(plus, "agents", name+".md")
	b, err := os.ReadFile(path)
	if err != nil {
		return VerifyMissing, "missing agents/" + name + ".md"
	}
	if len(strings.TrimSpace(string(b))) == 0 {
		return VerifyInvalid, "empty agent file"
	}
	// Re-use the agent frontmatter parser: a valid materialized agent carries a
	// frontmatter `name`. A legacy bare-prompt agent (no frontmatter) parses to an
	// empty name and is flagged invalid so the gate catches an unmaterialized
	// frontmatter (the materialize path always writes it).
	agName, _, _, _, _ := parseAgentFile(string(b))
	if agName == "" {
		return VerifyInvalid, "agent file missing frontmatter name"
	}
	var missing []string
	for _, d := range deps {
		if d != "" && !skillPresent[d] {
			missing = append(missing, d)
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return VerifyInvalid, "missing dependency skill(s): " + strings.Join(missing, ", ")
	}
	return VerifyOK, ""
}

// mcpVerifyStatus maps an MCP auth classification to a verify status: failed =>
// invalid (fails the gate), needs-auth => needs-auth (does NOT fail the gate but is
// surfaced with its command), ok => ok.
func mcpVerifyStatus(ar McpAuthReport) (VerifyStatus, string) {
	switch ar.Status {
	case McpAuthFailed:
		return VerifyMissing, "not materialized in .claude.json mcpServers"
	case McpAuthNeedsAuth:
		return VerifyNeedsAuth, "run: " + ar.Command
	default:
		return VerifyOK, ""
	}
}

// frontmatterField extracts a single top-level scalar field from a leading YAML
// `---` frontmatter block. It is intentionally tiny (the package carries no YAML
// dependency): it scans only the first frontmatter block for `key:` and returns the
// trimmed value, or "" if there is no frontmatter or no such key. Good enough for
// the presence checks the gate makes (name).
func frontmatterField(content, key string) string {
	norm := strings.ReplaceAll(content, "\r\n", "\n")
	if !strings.HasPrefix(norm, "---\n") {
		return ""
	}
	rest := norm[len("---\n"):]
	end := strings.Index(rest, "\n---")
	if end < 0 {
		return ""
	}
	fm := rest[:end]
	for _, line := range strings.Split(fm, "\n") {
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		if strings.TrimSpace(k) == key {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
