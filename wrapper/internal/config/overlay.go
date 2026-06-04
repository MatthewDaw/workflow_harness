package config

import (
	"io"
	"os"
	"path/filepath"
)

// The isolated config root for claude+ inner sessions (U21).
//
// claude+ launches its inner Claude with CLAUDE_CONFIG_DIR pointed at a STABLE
// ~/.claude+ directory, so the skills/agents bundled with the product and the
// session history claude+ generates live there instead of the user's personal
// ~/.claude. Because CLAUDE_CONFIG_DIR is set only on the child claude+ spawns, a
// normal `claude` (claude+ not running, or an unrelated session) reads ~/.claude
// and never sees this root.
//
// The root is stable (not per-session) and is NEVER torn down, so everything
// Claude keeps under its config dir — auth/credentials, onboarding state,
// transcripts, settings, MCP — persists across claude+ restarts. On first use it
// is seeded once from ~/.claude so the very first launch is already signed in;
// thereafter claude+ maintains its own copies.
//
// Layout:
//
//	~/.claude+/
//	  skills/        product-bundled skills (synced from HQ via ApplyPulled)
//	  agents/        product-bundled agents
//	  projects/      claude+ session transcripts (capture tailer reads here)
//	  .credentials.json, .claude.json, settings.json, .mcp.json
//	                 seeded once from ~/.claude, then owned by claude+

// seededFile is one file copied once from the user's ~/.claude into ~/.claude+ on
// first init, so claude+ starts authenticated/configured. src is relative to the
// home dir (some files live at ~/.claude/<name>, .claude.json lives at ~/<name>).
type seededFile struct {
	homeRel string // source path relative to $HOME
	name    string // destination file name under ~/.claude+
}

var seededFiles = []seededFile{
	{homeRel: filepath.Join(".claude", ".credentials.json"), name: ".credentials.json"},
	{homeRel: ".claude.json", name: ".claude.json"},
	{homeRel: filepath.Join(".claude", "settings.json"), name: "settings.json"},
	{homeRel: filepath.Join(".claude", ".mcp.json"), name: ".mcp.json"},
}

// plusDir resolves ~/.claude+.
func plusDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude+"), nil
}

// EnsureConfigDir returns the stable claude+ config root (~/.claude+), creating
// and seeding it on first use. Skills/agents dirs are ensured; auth + settings
// are copied once from ~/.claude (only when absent, so claude+'s own evolving
// state is never clobbered). It is idempotent and additive — safe to call on
// every session start — and never deletes anything, so auth and transcripts
// persist across restarts.
func EnsureConfigDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	plus := filepath.Join(home, ".claude+")
	if err := os.MkdirAll(plus, 0o755); err != nil {
		return "", err
	}
	for _, sub := range []string{"skills", "agents"} {
		_ = os.MkdirAll(filepath.Join(plus, sub), 0o755)
	}
	// Seed auth/settings from ~/.claude once, so the first claude+ launch is
	// already signed in. Skipped for any file claude+ already has.
	for _, f := range seededFiles {
		dst := filepath.Join(plus, f.name)
		if pathExists(dst) {
			continue
		}
		src := filepath.Join(home, f.homeRel)
		if pathExists(src) {
			_ = copyFile(src, dst)
		}
	}
	return plus, nil
}

// ConfigDir returns the claude+ config root if it has been initialized
// (~/.claude+ exists), reporting whether isolation is active. Callers that must
// locate Claude's config-relative files (e.g. the transcript tailer) use this so
// they read from the same root claude+ launches Claude against.
func ConfigDir() (string, bool) {
	plus, err := plusDir()
	if err != nil {
		return "", false
	}
	if _, statErr := os.Stat(plus); statErr != nil {
		return "", false
	}
	return plus, true
}

func pathExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Close()
}
