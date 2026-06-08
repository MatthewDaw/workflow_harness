package config

import (
	"bytes"
	"encoding/json"
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

// parseMemoryFrontmatter is a tiny purpose-built reader for exactly the keys a
// memory file carries, mirroring parseAgentFile in remote.go (we deliberately
// avoid pulling in a YAML dependency for three string fields). It differs from
// parseAgentFile in ONE way: `type` is NESTED under a `metadata:` block, not a
// flat top-level key, so we track when we are inside the metadata block and read
// its indented `type:` line. CRLF is normalized first so a Windows-authored file
// parses identically (the body is hashed/stored verbatim elsewhere; this parser
// only extracts the metadata, so normalizing here is safe).
//
// A file with no leading `---` frontmatter yields empty fields (the caller then
// defaults name to the filename); a malformed/unterminated block is tolerated by
// scanning only up to the closing fence we find (or end of text).
func parseMemoryFrontmatter(content string) (name, description, typ string) {
	norm := strings.ReplaceAll(content, "\r\n", "\n")
	if !strings.HasPrefix(norm, "---\n") {
		return "", "", ""
	}
	rest := norm[len("---\n"):]
	// Frontmatter ends at the next "\n---" fence; if absent, scan the whole rest.
	fm := rest
	if end := strings.Index(rest, "\n---"); end >= 0 {
		fm = rest[:end]
	}
	inMetadata := false
	for _, line := range strings.Split(fm, "\n") {
		indented := strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t")
		// A non-indented line resets the nested-block context: it opens the metadata
		// block only when it is exactly `metadata:` (header with no inline value);
		// any other top-level line closes it.
		if !indented {
			inMetadata = strings.TrimSpace(line) == "metadata:"
		}
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		val = strings.TrimSpace(val)
		switch key {
		case "name":
			// Only a top-level name is meaningful (ignore any stray nested key).
			if !indented {
				name = val
			}
		case "description":
			if !indented {
				description = val
			}
		case "type":
			// type is meaningful ONLY as metadata.type — an INDENTED line inside the
			// metadata block — so accept it only there (not a flat top-level type).
			if inMetadata && indented {
				typ = val
			}
		}
	}
	return name, description, typ
}

// ReconcileMemories uploads the caller's whole memory set to HQ with a single
// full-reconcile PUT (adds, updates, AND deletions all propagate server-side: the
// backend replaces the caller's entire set with `items`, so an item dropped from
// disk is dropped on HQ, and an empty array clears it). The array is ALWAYS sent —
// even when empty — because an empty body is how a project whose last memory was
// deleted reconciles to zero. Mirrors remote.go's auth/postJSON style (Bearer
// token, application/json, a 10s bounded client, non-2xx -> error).
func ReconcileMemories(baseURL, token, projectID string, items []MemoryItem) error {
	if items == nil {
		// Always send a concrete array, never JSON null, so the backend's
		// z.array(...) accepts it and a cleared project sends [].
		items = []MemoryItem{}
	}
	body, err := json.Marshal(map[string]any{"memories": items})
	if err != nil {
		return err
	}
	u := strings.TrimRight(baseURL, "/") + "/projects/" + url.PathEscape(projectID) + "/memories"
	req, err := http.NewRequest(http.MethodPut, u, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("content-type", "application/json")
	if token != "" {
		req.Header.Set("authorization", "Bearer "+token)
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return &httpStatusError{method: http.MethodPut, path: u, status: resp.Status}
	}
	return nil
}

// httpStatusError mirrors the fmt.Errorf("PUT %s: %s") shape remote.go uses for a
// non-2xx response, kept as a small typed error so the message is consistent.
type httpStatusError struct {
	method string
	path   string
	status string
}

func (e *httpStatusError) Error() string {
	return e.method + " " + e.path + ": " + e.status
}
