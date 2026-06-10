package config

// Shared YAML-frontmatter helpers for the agent, skill, and memory parsers.
// The package deliberately carries no YAML dependency: every consumer reads a
// handful of flat `key: value` lines (plus memory's one nested metadata block),
// so a fence splitter and a line scanner are all that is needed.

import "strings"

// normalizeNewlines folds CRLF to LF so a Windows-authored file parses
// identically (hashContent already normalizes line endings for hashing).
func normalizeNewlines(s string) string {
	return strings.ReplaceAll(s, "\r\n", "\n")
}

// frontmatterBlock splits a leading `---` frontmatter block off a CRLF-
// normalized copy of content, returning the block's inner text and the body
// after the closing fence. ok is false when content has no leading "---\n"
// line or no closing fence. The closing fence is either a full "\n---\n" line
// or a trailing "\n---" that ends the file (in which case the body is empty).
func frontmatterBlock(content string) (fm, body string, ok bool) {
	norm := normalizeNewlines(content)
	if !strings.HasPrefix(norm, "---\n") {
		return "", "", false
	}
	rest := norm[len("---\n"):]
	if end := strings.Index(rest, "\n---\n"); end >= 0 {
		return rest[:end], rest[end+len("\n---\n"):], true
	}
	if strings.HasSuffix(rest, "\n---") {
		return rest[:len(rest)-len("\n---")], "", true
	}
	return "", "", false
}

// frontmatterFields walks a frontmatter block line by line and calls fn for
// each `key: value` pair with the key and value whitespace-trimmed, plus
// whether the line was indented (nested under a block header such as memory's
// `metadata:`). Lines without a colon are skipped.
func frontmatterFields(fm string, fn func(key, val string, indented bool)) {
	for _, line := range strings.Split(fm, "\n") {
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		indented := strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t")
		fn(strings.TrimSpace(key), strings.TrimSpace(val), indented)
	}
}

// frontmatterField extracts a single top-level scalar field from a leading
// frontmatter block: the trimmed value of the first `key:` line, or "" when
// there is no complete frontmatter or no such key. Good enough for the
// presence checks the verify gate makes (name).
func frontmatterField(content, key string) string {
	fm, _, ok := frontmatterBlock(content)
	if !ok {
		return ""
	}
	val, found := "", false
	frontmatterFields(fm, func(k, v string, _ bool) {
		if !found && k == key {
			val, found = v, true
		}
	})
	return val
}
