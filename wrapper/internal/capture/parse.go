package capture

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strings"

	"github.com/workflow-harness/claude-plus/internal/config"
)

// TranscriptPath resolves the JSONL transcript path for a session in a repo,
// mirroring Claude Code's <config>/projects/<hash>/<sid>.jsonl layout. claude+
// launches Claude against the isolated ~/.claude+ config root (U21), so when that
// root is active the transcript lives under it — reading ~/.claude here would
// miss every event and the session would never reach HQ. Falls back to ~/.claude
// when isolation is not active. The project hash is derived from the absolute
// repo path. This is the same surface ce-sessions reads (KTD3).
func TranscriptPath(repoRoot, sessionID string) (string, error) {
	base, ok := config.ConfigDir(repoRoot)
	if !ok {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".claude")
	}
	return filepath.Join(base, "projects", projectHash(repoRoot), sessionID+".jsonl"), nil
}

// MemoryDir resolves the per-project memory directory for a repo, the sibling
// `memory/` under the SAME <config>/projects/<hash>/ dir the transcript lives in
// (TranscriptPath). claude+ launches Claude against the isolated ~/.claude+ config
// root (U21), so when isolation is active the memories live under it; we fall back
// to ~/.claude otherwise — exactly mirroring TranscriptPath's base resolution so
// the two never diverge. The <hash> is projectHash(repoRoot), reused (not
// re-derived) so a Claude Code layout change is re-pinned in one place (R2).
func MemoryDir(repoRoot string) (string, error) {
	base, ok := config.ConfigDir(repoRoot)
	if !ok {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".claude")
	}
	return filepath.Join(base, "projects", projectHash(repoRoot), "memory"), nil
}

// projectHash derives Claude Code's per-project directory name. Claude Code
// slugifies the absolute path by replacing every non-alphanumeric character with
// a dash (NOT just path separators): `C:\Users\me\workflow_harness` becomes
// `C--Users-me-workflow-harness` — note the underscore also becomes a dash, and
// runs of specials are NOT collapsed. Replacing only `/ \ :` (the old behavior)
// missed the underscore, so the tailer watched a directory that never exists and
// no transcript events were ever captured. Isolated here so it can be re-pinned
// if the layout changes (R2).
func projectHash(repoRoot string) string {
	slug := slugifyPath(repoRoot)
	slug = strings.TrimPrefix(slug, "-")
	if slug == "" {
		sum := sha1.Sum([]byte(repoRoot))
		return hex.EncodeToString(sum[:])[:16]
	}
	return slug
}

// slugifyPath maps every non-alphanumeric rune to a dash, mirroring Claude Code's
// project-directory naming. Specials are mapped 1:1 (no collapsing), so `C:\` ->
// `C--`.
func slugifyPath(p string) string {
	var b strings.Builder
	b.Grow(len(p))
	for _, r := range p {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	return b.String()
}

// maxContentChars caps any single carried-content field (user/assistant text,
// tool args, tool result) so a giant transcript block can't blow up an envelope
// or the table item. Mirrors MAX_CONTENT_CHARS in packages/shared.
const maxContentChars = 8000

// asString reports whether a content payload is a plain JSON string and returns
// its value. Current user prompts are carried as a bare string.
func asString(raw json.RawMessage) (string, bool) {
	if len(raw) == 0 {
		return "", false
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s, true
	}
	return "", false
}

// parseBlocks decodes a `message.content` array into its blocks. A non-array
// (e.g. a plain string) yields nil so callers can fall back to asString.
func parseBlocks(raw json.RawMessage) []block {
	if len(raw) == 0 {
		return nil
	}
	var blocks []block
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil
	}
	return blocks
}

// textFromContent pulls human-readable text from a tool_result `content`, which
// may be a plain string or an array of `{type:"text", text:"…"}` blocks.
func textFromContent(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	if s, ok := asString(raw); ok {
		return strings.TrimSpace(s)
	}
	var blocks []block
	if err := json.Unmarshal(raw, &blocks); err == nil {
		var parts []string
		for _, b := range blocks {
			if b.Type == "text" && b.Text != "" {
				parts = append(parts, b.Text)
			}
		}
		return strings.TrimSpace(strings.Join(parts, " "))
	}
	return ""
}

// capContent truncates carried content to maxContentChars (rune-safe), so we
// forward the REAL content but never an unbounded blob.
func capContent(s string) string {
	s = strings.TrimSpace(s)
	if len(s) <= maxContentChars {
		return s
	}
	return truncate(s, maxContentChars)
}

// tokenCount is a cheap heuristic (~4 chars/token) used only when the transcript
// row carries no usage block (e.g. user turns). Server-side usage wins when
// present.
func tokenCount(text string) int64 {
	if text == "" {
		return 0
	}
	n := int64(len(text) / 4)
	if n == 0 {
		n = 1
	}
	return n
}

// argsSummaryCap bounds a tool.call argsSummary. Larger than the old 80 so the
// real command/args are visible in the HQ feed, but still bounded.
const argsSummaryCap = 2000

// summarizeInput renders the tool's input for the tool.call argsSummary field:
// the most descriptive common field when present, otherwise the compact JSON.
// Capped at argsSummaryCap.
func summarizeInput(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return truncate(string(raw), argsSummaryCap)
	}
	// Prefer the most descriptive common fields.
	for _, k := range []string{"command", "file_path", "path", "pattern", "query", "url"} {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok && s != "" {
				return truncate(s, argsSummaryCap)
			}
		}
	}
	b, _ := json.Marshal(m)
	return truncate(string(b), argsSummaryCap)
}

// truncate shortens s to at most n runes, appending an ellipsis when cut. It is
// rune-safe so a multibyte UTF-8 character is never split.
func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if n <= 0 {
		return ""
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

// round2 rounds USD to cents to keep envelopes tidy.
func round2(f float64) float64 { return math.Round(f*100) / 100 }
