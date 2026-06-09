package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// PruneToEffective makes the per-project root a TIGHT mirror of Command HQ for
// SKILLS and AGENTS: it DELETES, from `plus` (the per-project config root ONLY),
// any skill directory or agent file whose name is neither in HQ's effective
// enabled set (`remote`) NOR provided by the connected repo's own `.claude`
// (project skills/agents a normal Claude session reads from the working tree —
// those are "normal claude skills" and are left alone). This is what turns
// `claude+ sync-skills` from additive into an exact mirror: a skill removed from
// the org catalog / project enabled set stops lingering locally.
//
// HARD SAFETY RULES:
//   - It only ever deletes UNDER `plus`. It NEVER reads or writes the user's
//     personal `~/.claude`, and it refuses to run if `plus` does not look like a
//     per-project root (must live under a `roots/` segment), so a bad caller can
//     never wipe the wrong tree.
//   - The caller MUST pass `remote` from a SUCCESSFUL `src.Fetch()`. Pruning on a
//     failed / 401 / transient-empty fetch would delete everything — so the sync
//     path gates this behind a fetch that returned without error.
//   - MCP servers are deliberately NOT pruned here: the root's `.claude.json`
//     `mcpServers` also holds user-authed claude.ai connectors that are not
//     HQ-managed, and deleting those would break a live integration.
//
// Returns the number of items removed and any per-item errors (non-fatal).
func PruneToEffective(plus, repoRoot string, remote []RemoteItem) (removed int, errs []error) {
	// Safety: only operate on a per-project root (…/.claude+/roots/<slug>). This
	// guards against ever being pointed at ~/.claude or a home dir by mistake.
	if plus == "" || !strings.Contains(filepath.ToSlash(plus), "/roots/") {
		return 0, []error{fmt.Errorf("prune refused: %q is not a per-project root", plus)}
	}

	keepSkills := map[string]bool{}
	keepAgents := map[string]bool{}
	keepWorkflows := map[string]bool{}
	for _, r := range remote {
		switch r.Kind {
		case KindSkill:
			keepSkills[r.Name] = true
		case KindAgent:
			keepAgents[r.Name] = true
		case KindWorkflow:
			keepWorkflows[r.Name] = true
		}
	}
	// Union in the connected repo's OWN project skills/agents (read from the repo
	// working tree by a normal Claude session) so a tight HQ sync never deletes the
	// project's own definitions.
	if repoRoot != "" {
		repoClaude := filepath.Join(repoRoot, ".claude")
		for _, n := range childDirNames(filepath.Join(repoClaude, "skills")) {
			keepSkills[n] = true
		}
		for _, n := range mdBaseNames(filepath.Join(repoClaude, "agents")) {
			keepAgents[n] = true
		}
		for _, n := range jsonBaseNames(filepath.Join(repoClaude, "workflows")) {
			keepWorkflows[n] = true
		}
	}

	// Prune the ROOT's skill directories not in the keep set.
	skillsDir := filepath.Join(plus, "skills")
	for _, name := range childDirNames(skillsDir) {
		if keepSkills[name] {
			continue
		}
		if err := os.RemoveAll(filepath.Join(skillsDir, name)); err != nil {
			errs = append(errs, fmt.Errorf("prune skill %q: %w", name, err))
			continue
		}
		removed++
	}

	// Prune the ROOT's agent files (*.md) not in the keep set.
	agentsDir := filepath.Join(plus, "agents")
	for _, name := range mdBaseNames(agentsDir) {
		if keepAgents[name] {
			continue
		}
		if err := os.Remove(filepath.Join(agentsDir, name+".md")); err != nil {
			errs = append(errs, fmt.Errorf("prune agent %q: %w", name, err))
			continue
		}
		removed++
	}

	// Prune the ROOT's workflow files (*.json) not in the keep set, mirroring agents.
	workflowsDir := filepath.Join(plus, "workflows")
	for _, name := range jsonBaseNames(workflowsDir) {
		if keepWorkflows[name] {
			continue
		}
		if err := os.Remove(filepath.Join(workflowsDir, name+".json")); err != nil {
			errs = append(errs, fmt.Errorf("prune workflow %q: %w", name, err))
			continue
		}
		removed++
	}

	return removed, errs
}

// childDirNames returns the names of immediate subdirectories of dir (a missing
// dir yields nothing).
func childDirNames(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			out = append(out, e.Name())
		}
	}
	return out
}

// mdBaseNames returns the basenames (without the .md extension) of *.md files in
// dir (a missing dir yields nothing).
func mdBaseNames(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".md") {
			out = append(out, strings.TrimSuffix(e.Name(), ".md"))
		}
	}
	return out
}

// jsonBaseNames returns the basenames (without the .json extension) of *.json
// files in dir (a missing dir yields nothing). The workflow analog of mdBaseNames.
func jsonBaseNames(dir string) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".json") {
			out = append(out, strings.TrimSuffix(e.Name(), ".json"))
		}
	}
	return out
}
