package config

import (
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// MemoryItem is one Claude Code project "memory" as the wrapper reads it off disk
// and uploads it to HQ. It is the on-disk shape of a single memory/<slug>.md file:
// the frontmatter's name/description/type plus the FULL raw file text as Content
// (so HQ stores the verbatim markdown the user/Claude authored, frontmatter and
// all — same principle as a skill's body being the whole SKILL.md). The JSON tags
// match the backend's memoryInputSchema ({name, description?, type?, content}) so
// a slice of these serializes straight into the PUT /projects/{id}/memories body.
type MemoryItem struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	Type        string `json:"type,omitempty"`
	Content     string `json:"content"`
}

// ReadLocalMemories scans memoryDir (NON-recursive) for the per-project memory
// files Claude writes — memory/<slug>.md — and returns one MemoryItem per file.
// MEMORY.md (the human-facing index Claude maintains alongside the slugged files)
// is skipped case-insensitively, as is anything that is not a .md file. Each file
// is parsed for its frontmatter name/description/metadata.type and its Content is
// set to the FULL raw file text (so HQ keeps the verbatim markdown, not just the
// body). When the frontmatter omits name we default to the filename without .md,
// matching how Claude slugs the file in the first place.
//
// A missing memoryDir is NOT an error: a project that has never had a memory saved
// simply has no memory/ dir, and that must reconcile to an empty set (clearing the
// author's memories on HQ), not fail the whole sync. So an absent dir returns an
// empty slice + nil.
func ReadLocalMemories(memoryDir string) ([]MemoryItem, error) {
	entries, err := os.ReadDir(memoryDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil // no memories yet — a normal, empty reconcile, not an error
		}
		return nil, err
	}
	var out []MemoryItem
	for _, e := range entries {
		if e.IsDir() {
			continue // memories are flat files; ignore any nested dirs
		}
		name := e.Name()
		// Skip the MEMORY.md index (case-insensitive) and any non-.md file.
		if strings.EqualFold(name, "MEMORY.md") {
			continue
		}
		if !strings.HasSuffix(strings.ToLower(name), ".md") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(memoryDir, name))
		if err != nil {
			// A single unreadable file must not abort the whole reconcile (a partial
			// upload is better than dropping every memory); skip it.
			continue
		}
		raw := string(b)
		mname, desc, typ := parseMemoryFrontmatter(raw)
		// Default the name to the filename without .md when the frontmatter omits it
		// (this is how Claude names the file in the first place).
		if strings.TrimSpace(mname) == "" {
			mname = strings.TrimSuffix(name, filepath.Ext(name))
		}
		out = append(out, MemoryItem{
			Name:        mname,
			Description: desc,
			Type:        typ,
			Content:     raw,
		})
	}
	return out, nil
}

// parseMemoryFrontmatter reads exactly the keys a memory file carries via the
// shared frontmatter.go helpers. It differs from parseAgentFile in ONE way:
// `type` is NESTED under a `metadata:` block, not a flat top-level key, so we
// track when we are inside the metadata block and read only its indented
// `type:` line (a flat top-level `type` is ignored, as are stray nested
// name/description keys).
//
// A file with no leading `---` frontmatter yields empty fields (the caller then
// defaults name to the filename); an unterminated block is tolerated by
// scanning the whole rest of the text.
func parseMemoryFrontmatter(content string) (name, description, typ string) {
	norm := normalizeNewlines(content)
	if !strings.HasPrefix(norm, "---\n") {
		return "", "", ""
	}
	fm, _, ok := frontmatterBlock(content)
	if !ok {
		fm = norm[len("---\n"):]
	}
	inMetadata := false
	frontmatterFields(fm, func(key, val string, indented bool) {
		// A non-indented line resets the nested-block context: it opens the metadata
		// block only when it is exactly `metadata:` (header with no inline value);
		// any other top-level line closes it.
		if !indented {
			inMetadata = key == "metadata" && val == ""
		}
		switch key {
		case "name":
			if !indented {
				name = val
			}
		case "description":
			if !indented {
				description = val
			}
		case "type":
			if inMetadata && indented {
				typ = val
			}
		}
	})
	return name, description, typ
}

// ReconcileMemories uploads the caller's whole memory set to HQ with a single
// full-reconcile PUT (adds, updates, AND deletions all propagate server-side: the
// backend replaces the caller's entire set with `items`, so an item dropped from
// disk is dropped on HQ, and an empty array clears it). The array is ALWAYS sent —
// even when empty — because an empty body is how a project whose last memory was
// deleted reconciles to zero.
func ReconcileMemories(baseURL, token, projectID string, items []MemoryItem) error {
	if items == nil {
		// Always send a concrete array, never JSON null, so the backend's
		// z.array(...) accepts it and a cleared project sends [].
		items = []MemoryItem{}
	}
	u := strings.TrimRight(baseURL, "/") + "/projects/" + url.PathEscape(projectID) + "/memories"
	client := &http.Client{Timeout: 10 * time.Second}
	return DoJSON(client, http.MethodPut, u, token, map[string]any{"memories": items}, nil, u)
}
