// Package config keeps the wrapper's Agents/Skills view in sync with the local
// ~/.claude setup and the HQ scoped registry (U17). It reads local agent/skill
// definitions, diffs them against HQ's effective set for this user+project, and
// surfaces drift with a one-key reconcile. Writes are additive and reversible.
package config

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Kind distinguishes the two local registry types.
type Kind string

const (
	KindAgent Kind = "agent"
	KindSkill Kind = "skill"
	// KindMcp is an MCP server, which (unlike agents/skills) does not live
	// one-file-per-item: every server is an entry merged into the per-project
	// <root>/.claude.json mcpServers map (U-MCP-Target). Diff/Reconcile/DriftReport
	// are kind-generic and need no change; the difference is confined to
	// ReadLocal/ApplyPulled and mcp.go.
	KindMcp Kind = "mcp"
)

// Item is a local ~/.claude definition (an agent or a skill), identified by name
// with a content hash used for drift detection.
type Item struct {
	Kind Kind   `json:"kind"`
	Name string `json:"name"`
	Path string `json:"path"`
	Hash string `json:"hash"` // sha256 of normalized content
	Err  string `json:"err,omitempty"` // set if the file was malformed
}

// claudeDir resolves ~/.claude.
func claudeDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude"), nil
}

// ReadLocal reads all local agents and skills, over the union of the user's own
// registry (~/.claude) and the given isolated claude+ registry (`plus`, a repo's
// per-project root — see overlay.go's ProjectConfigDir). Reading both means
// drift is computed over everything the inner Claude can see, while a pull only
// ever writes into `plus` (ApplyPulled), so bundled skills never pollute the
// user's personal ~/.claude. On a name collision the user's own ~/.claude entry
// wins. Malformed files are reported (Err set) rather than aborting the scan.
func ReadLocal(plus string) ([]Item, error) {
	userDir, err := claudeDir()
	if err != nil {
		return nil, err
	}

	var items []Item
	items = append(items, readDir(filepath.Join(userDir, "agents"), KindAgent)...)
	items = append(items, readDir(filepath.Join(userDir, "skills"), KindSkill)...)
	// MCP servers are not files-per-item: emit one Item per mcpServers entry in
	// ~/.claude/.claude.json. A malformed file surfaces as a single Err item (never
	// a panic) and must not blank the other kinds. (NOTE: ~/.claude/.claude.json
	// rarely exists — the user's real one is at ~/.claude.json — but reading it is
	// harmless: an absent file yields nothing, and the per-project root's
	// .claude.json, read via `plus` below, is the one ApplyPulled writes.)
	items = append(items, readMcpFile(filepath.Join(userDir, mcpFileName))...)

	// Union in the isolated claude+ registry, skipping names already provided by
	// the user's own registry (~/.claude wins on collision).
	seen := map[string]bool{}
	for _, it := range items {
		seen[string(it.Kind)+"/"+it.Name] = true
	}
	plusItems := append(
		readDir(filepath.Join(plus, "agents"), KindAgent),
		readDir(filepath.Join(plus, "skills"), KindSkill)...,
	)
	plusItems = append(plusItems, readMcpFile(filepath.Join(plus, mcpFileName))...)
	for _, it := range plusItems {
		if seen[string(it.Kind)+"/"+it.Name] {
			continue
		}
		items = append(items, it)
	}

	sort.Slice(items, func(i, j int) bool {
		if items[i].Kind != items[j].Kind {
			return items[i].Kind < items[j].Kind
		}
		return items[i].Name < items[j].Name
	})
	return items, nil
}

// readDir scans a single registry directory. Agents are *.md files; skills are
// either *.md files or directories containing a SKILL.md plus optional sibling
// scripts/resources. For a directory-form skill the hash covers the WHOLE tree
// (U-Skill-Dirs) via hashSkillFiles, so editing a sibling file drifts — not just
// SKILL.md. A single-SKILL.md directory hashes byte-identically to the legacy
// raw-body form (skillFilesCanonical's fast path), so legacy skills stay in-sync.
func readDir(root string, kind Kind) []Item {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil // directory absent -> no items
	}
	var out []Item
	for _, e := range entries {
		name := strings.TrimSuffix(e.Name(), ".md")
		if e.IsDir() {
			out = append(out, readSkillDirItem(root, e.Name(), kind))
			continue
		}
		if !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		path := filepath.Join(root, e.Name())
		it := Item{Kind: kind, Name: name, Path: path}
		b, err := os.ReadFile(path)
		if err != nil {
			it.Err = "unreadable: " + err.Error()
			out = append(out, it)
			continue
		}
		if len(strings.TrimSpace(string(b))) == 0 {
			it.Err = "empty definition"
		}
		it.Hash = hashContent(b)
		out = append(out, it)
	}
	return out
}

// readSkillDirItem builds the Item for a directory-form skill: it walks the whole
// tree (so sibling edits drift) and hashes it via hashSkillFiles. Path points at
// the skill's SKILL.md (the entry file the verify gate and UI reference). A skill
// directory missing SKILL.md, an empty SKILL.md, or an unreadable file is flagged
// as an Err item rather than silently hashing a partial/invalid tree.
func readSkillDirItem(root, dirName string, kind Kind) Item {
	name := dirName
	skillRoot := filepath.Join(root, dirName)
	it := Item{Kind: kind, Name: name, Path: filepath.Join(skillRoot, skillMainFile)}
	files, ok := readSkillDir(skillRoot)
	if !ok {
		it.Err = "unreadable skill directory"
		return it
	}
	main, hasMain := files[skillMainFile]
	if !hasMain {
		it.Err = "missing " + skillMainFile
		return it
	}
	if len(strings.TrimSpace(main)) == 0 {
		it.Err = "empty definition"
	}
	it.Hash = hashSkillFiles(files)
	return it
}

// readMcpFile reads a .claude.json file and emits one Item{Kind: KindMcp} per
// mcpServers entry, with Hash computed from the entry's canonical serialization
// (so an on-disk server compares equal to its HQ-built twin). Path points at the
// shared .claude.json file. An absent file yields nothing. A malformed file
// surfaces as a SINGLE Err item (named after the file) rather than a panic, and —
// because it is one item — never blanks the agent/skill items collected alongside
// it. A per-entry that fails to canonicalize is likewise flagged as an Err item
// for that server name only.
func readMcpFile(path string) []Item {
	mf, err := parseMcpFile(path)
	if err != nil {
		return []Item{{Kind: KindMcp, Name: mcpFileName, Path: path, Err: "malformed .claude.json: " + err.Error()}}
	}
	out := make([]Item, 0, len(mf.McpServers))
	for name, raw := range mf.McpServers {
		it := Item{Kind: KindMcp, Name: name, Path: path}
		hash, hErr := canonicalServerHash(raw)
		if hErr != nil {
			it.Err = "malformed entry: " + hErr.Error()
		} else {
			it.Hash = hash
		}
		out = append(out, it)
	}
	return out
}

// ApplyPulled materializes an HQ item into the given isolated claude+ tree
// (`dir`, a repo's per-project root): agents land at agents/<name>.md, skills at
// skills/<name>/SKILL.md. It writes into `dir`, never the user's personal
// ~/.claude, so pulled product-bundled skills stay out of their normal Claude
// dataset. Parent directories are created as needed. The write is additive and
// reversible (a pull never deletes other definitions), and writing exactly
// `body` keeps the local hash equal to the HQ hash, so a freshly pulled item
// reads back as in-sync (reconcile is idempotent).
func ApplyPulled(dir string, item RemoteItem, body string) error {
	var path string
	switch item.Kind {
	case KindAgent:
		path = filepath.Join(dir, "agents", item.Name+".md")
	case KindSkill:
		// Skills can ship a WHOLE directory (SKILL.md plus sibling scripts/resources)
		// carried in the body envelope (U-Skill-Dirs). Decode the files map (a legacy
		// body-only skill decodes to just {"SKILL.md": body}) and materialize the
		// whole tree. writeSkillDir is additive and matches the hash skillFilesCanonical
		// computes, so a freshly pulled skill reads back in-sync.
		skillRoot := filepath.Join(dir, "skills", item.Name)
		if err := os.MkdirAll(skillRoot, 0o755); err != nil {
			return err
		}
		return writeSkillDir(skillRoot, decodeSkillBody(body))
	case KindMcp:
		// MCP servers MERGE into .mcp.json rather than overwrite a per-item file:
		// the body is the canonical on-disk entry JSON (built by remote.Fetch from
		// the structured HQ record). mergeMcpServer preserves every other server
		// and unrelated top-level key, and writes the entry canonically so a
		// re-read hashes identically (idempotent pull).
		var entry mcpEntry
		if err := json.Unmarshal([]byte(body), &entry); err != nil {
			return fmt.Errorf("decode mcp entry %q: %w", item.Name, err)
		}
		return mergeMcpServer(filepath.Join(dir, mcpFileName), item.Name, entry)
	default:
		return fmt.Errorf("unknown kind %q", item.Kind)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(body), 0o644)
}

// hashContent normalizes line endings then hashes, so CRLF/LF differences do not
// register as drift.
func hashContent(b []byte) string {
	norm := strings.ReplaceAll(string(b), "\r\n", "\n")
	sum := sha256.Sum256([]byte(strings.TrimSpace(norm)))
	return hex.EncodeToString(sum[:])
}
