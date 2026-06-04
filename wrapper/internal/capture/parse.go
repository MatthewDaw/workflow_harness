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
	base, ok := config.ConfigDir()
	if !ok {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".claude")
	}
	return filepath.Join(base, "projects", projectHash(repoRoot), sessionID+".jsonl"), nil
}

// projectHash derives Claude Code's per-project directory name. Claude Code
// slugifies the absolute path (replacing path separators); we additionally hash
// for a stable fallback. Isolated here so it can be re-pinned if the layout
// changes (R2).
func projectHash(repoRoot string) string {
	// Claude Code uses a dash-slugged absolute path as the directory name.
	slug := strings.NewReplacer("/", "-", "\\", "-", ":", "-").Replace(repoRoot)
	slug = strings.TrimPrefix(slug, "-")
	if slug == "" {
		sum := sha1.Sum([]byte(repoRoot))
		return hex.EncodeToString(sum[:])[:16]
	}
	return slug
}

// contentBlock is one element of a message content array (text or tool blocks).
type contentBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// messagePayload is the nested message object in user/assistant rows.
type messagePayload struct {
	Content json.RawMessage `json:"content"`
}

// extractText pulls human-readable text from a message/content payload, which
// may be a plain string or an array of content blocks.
func extractText(message, content json.RawMessage) string {
	if len(message) > 0 {
		var mp messagePayload
		if err := json.Unmarshal(message, &mp); err == nil && len(mp.Content) > 0 {
			if s := textFromContent(mp.Content); s != "" {
				return s
			}
		}
	}
	return textFromContent(content)
}

// textFromContent handles both `"a string"` and `[{type,text}, ...]`.
func textFromContent(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return strings.TrimSpace(s)
	}
	var blocks []contentBlock
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

// summarizeInput renders a short, single-line summary of a tool's input for the
// tool.call event argsSummary field.
func summarizeInput(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return truncate(string(raw), 80)
	}
	// Prefer the most descriptive common fields.
	for _, k := range []string{"file_path", "path", "command", "pattern", "query", "url"} {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok && s != "" {
				return truncate(s, 80)
			}
		}
	}
	b, _ := json.Marshal(m)
	return truncate(string(b), 80)
}

// summarizeContent renders a short summary of a tool result.
func summarizeContent(raw json.RawMessage) string {
	s := textFromContent(raw)
	if s == "" {
		s = truncate(string(raw), 80)
	}
	return truncate(s, 120)
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

// round2 rounds USD to cents to keep envelopes tidy.
func round2(f float64) float64 { return math.Round(f*100) / 100 }
