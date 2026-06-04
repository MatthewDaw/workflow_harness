// Package config keeps the wrapper's Agents/Skills view in sync with the local
// ~/.claude setup and the HQ scoped registry (U17). It reads local agent/skill
// definitions, diffs them against HQ's effective set for this user+project, and
// surfaces drift with a one-key reconcile. Writes are additive and reversible.
package config

import (
	"crypto/sha256"
	"encoding/hex"
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
// registry (~/.claude) and the isolated claude+ registry (~/.claude+, where
// product-bundled skills live — see overlay.go). Reading both means drift is
// computed over everything the inner Claude can see, while a pull only ever
// writes into ~/.claude+ (ApplyPulled), so bundled skills never pollute the
// user's personal ~/.claude. On a name collision the user's own ~/.claude entry
// wins. Malformed files are reported (Err set) rather than aborting the scan.
func ReadLocal() ([]Item, error) {
	userDir, err := claudeDir()
	if err != nil {
		return nil, err
	}
	plus, err := plusDir()
	if err != nil {
		return nil, err
	}

	var items []Item
	items = append(items, readDir(filepath.Join(userDir, "agents"), KindAgent)...)
	items = append(items, readDir(filepath.Join(userDir, "skills"), KindSkill)...)

	// Union in the isolated claude+ registry, skipping names already provided by
	// the user's own registry (~/.claude wins on collision).
	seen := map[string]bool{}
	for _, it := range items {
		seen[string(it.Kind)+"/"+it.Name] = true
	}
	for _, it := range append(
		readDir(filepath.Join(plus, "agents"), KindAgent),
		readDir(filepath.Join(plus, "skills"), KindSkill)...,
	) {
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
// either *.md files or directories containing a SKILL.md.
func readDir(root string, kind Kind) []Item {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil // directory absent -> no items
	}
	var out []Item
	for _, e := range entries {
		name := strings.TrimSuffix(e.Name(), ".md")
		var path string
		if e.IsDir() {
			path = filepath.Join(root, e.Name(), "SKILL.md")
		} else if strings.HasSuffix(e.Name(), ".md") {
			path = filepath.Join(root, e.Name())
		} else {
			continue
		}
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

// ApplyPulled materializes an HQ item into the isolated claude+ tree
// (~/.claude+): agents land at agents/<name>.md, skills at skills/<name>/SKILL.md.
// It writes into ~/.claude+, never the user's personal ~/.claude, so pulled
// product-bundled skills stay out of their normal Claude dataset. Parent
// directories are created as needed. The write is additive and reversible (a pull
// never deletes other definitions), and writing exactly `body` keeps the local
// hash equal to the HQ hash, so a freshly pulled item reads back as in-sync
// (reconcile is idempotent).
func ApplyPulled(item RemoteItem, body string) error {
	dir, err := plusDir()
	if err != nil {
		return err
	}
	var path string
	switch item.Kind {
	case KindAgent:
		path = filepath.Join(dir, "agents", item.Name+".md")
	case KindSkill:
		path = filepath.Join(dir, "skills", item.Name, "SKILL.md")
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
