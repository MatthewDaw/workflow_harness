package config

import (
	"crypto/sha1"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// The isolated config root for claude+ inner sessions (U21), now PER-PROJECT.
//
// claude+ launches its inner Claude with CLAUDE_CONFIG_DIR pointed at a config
// root under a STABLE ~/.claude+ base, so the skills/agents/MCP bundled with the
// product and the session history claude+ generates live there instead of the
// user's personal ~/.claude. Because CLAUDE_CONFIG_DIR is set only on the child
// claude+ spawns, a normal `claude` (claude+ not running, or an unrelated
// session) reads ~/.claude and never sees this root.
//
// Layout:
//
//	~/.claude+/                       BASE — canonical auth/identity (machine-wide)
//	  .credentials.json .claude.json settings.json .mcp.json
//	                                  seeded once from ~/.claude; where re-login lands
//	  roots/<projectSlug>/            PER-PROJECT root = a session's CLAUDE_CONFIG_DIR
//	    skills/ agents/ .mcp.json     scoped to THIS project's enabled set only
//	    projects/                     this repo's transcripts/memory
//	    .credentials.json .claude.json settings.json
//	                                  auth kept in sync with the BASE (see below)
//
// Why per-project: the local skills/agents/MCP reconcile is additive and is
// scoped per project at PULL time (the HQ source is built with the project id).
// A single shared root would accumulate the UNION of every project ever synced
// and never prune, so a session would read skills enabled on other projects.
// Giving each repo its own root means a session reads ONLY its project's set,
// and two repos' concurrent sessions never collide.
//
// Auth sharing: the BASE is the source of truth for Claude auth. Each spawn runs
// a bidirectional newer-wins sync of the auth files between the BASE and the
// project root (syncAuthFile): a base that is newer (a fresh `claude+ login` /
// another project's refreshed token written back) flows down into the root, and
// a root whose token Claude refreshed in-session flows back up to the base — so
// every project converges through the base hub without one repo's token refresh
// logging another out. `.mcp.json` is NOT an auth file: it is seeded into the
// root ONCE (so the project's pulled MCP servers, merged in by ApplyPulled,
// persist) and then owned per-project.

// seededFile is one file copied once from the user's ~/.claude into the BASE
// ~/.claude+ on first init, so claude+ starts authenticated/configured. src is
// relative to the home dir (some files live at ~/.claude/<name>, .claude.json
// lives at ~/<name>).
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

// authSyncFiles are the per-project root files kept convergent with the BASE on
// every spawn (newer-wins, both directions). `.mcp.json` is deliberately absent:
// it is seeded once and then owned per-project so pulled MCP servers persist.
var authSyncFiles = []string{".credentials.json", ".claude.json", "settings.json"}

// plusDir resolves the BASE ~/.claude+ (canonical auth + the parent of every
// per-project root). Kept as the package's single home-relative anchor.
func plusDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude+"), nil
}

// projectSlug derives a stable, readable, collision-free directory name for a
// repo's per-project root. It is the lowercased base folder name (slugified)
// plus an 8-hex suffix of the absolute path's SHA-1, so two repos that share a
// folder name in different locations never collide. Self-contained in this
// package (no import of the daemon/capture slug helpers).
func projectSlug(repoRoot string) string {
	base := strings.ToLower(filepath.Base(repoRoot))
	var b strings.Builder
	prevDash := false
	for _, r := range base {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			prevDash = false
		} else if !prevDash {
			b.WriteByte('-')
			prevDash = true
		}
	}
	slug := strings.Trim(b.String(), "-")
	sum := sha1.Sum([]byte(repoRoot))
	suffix := hex.EncodeToString(sum[:])[:8]
	if slug == "" {
		return suffix
	}
	return slug + "-" + suffix
}

// ProjectConfigDir resolves a repo's per-project config root path
// (~/.claude+/roots/<projectSlug>) WITHOUT creating or seeding it. It is the
// pure-path resolver the read/sync paths use (ReadLocal/ApplyPulled callers,
// the drift meter, the transcript tailer); EnsureConfigDir is the side-effecting
// variant used at spawn.
func ProjectConfigDir(repoRoot string) (string, error) {
	base, err := plusDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, "roots", projectSlug(repoRoot)), nil
}

// EnsureBaseDir returns the stable BASE config root (~/.claude+), creating and
// seeding it on first use. Auth + settings are copied once from ~/.claude (only
// when absent, so claude+'s own evolving state is never clobbered). It is
// idempotent and additive — safe to call on every session start — and never
// deletes anything. Used directly by InstallHooks (hooks live in the base
// settings.json and propagate to each project root via the auth sync).
func EnsureBaseDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	base := filepath.Join(home, ".claude+")
	if err := os.MkdirAll(base, 0o755); err != nil {
		return "", err
	}
	for _, sub := range []string{"skills", "agents"} {
		_ = os.MkdirAll(filepath.Join(base, sub), 0o755)
	}
	// Seed auth/settings from ~/.claude once, so the first claude+ launch is
	// already signed in. Skipped for any file the base already has.
	for _, f := range seededFiles {
		dst := filepath.Join(base, f.name)
		if pathExists(dst) {
			continue
		}
		src := filepath.Join(home, f.homeRel)
		if pathExists(src) {
			_ = copyFile(src, dst)
		}
	}
	return base, nil
}

// EnsureConfigDir returns the PER-PROJECT config root for repoRoot
// (~/.claude+/roots/<slug>), creating it and reconciling its auth with the BASE.
// This is the CLAUDE_CONFIG_DIR a session for repoRoot is launched against. A
// failure here must never block a session; callers fall back to ~/.claude.
func EnsureConfigDir(repoRoot string) (string, error) {
	base, err := EnsureBaseDir()
	if err != nil {
		return "", err
	}
	root := filepath.Join(base, "roots", projectSlug(repoRoot))
	if err := os.MkdirAll(root, 0o755); err != nil {
		return "", err
	}
	for _, sub := range []string{"skills", "agents"} {
		_ = os.MkdirAll(filepath.Join(root, sub), 0o755)
	}
	// `.mcp.json` is seeded ONCE from the base (which carries the user's personal
	// MCP servers) so the project's pulled HQ servers merged in afterwards are not
	// clobbered on later spawns.
	rootMcp := filepath.Join(root, ".mcp.json")
	if !pathExists(rootMcp) {
		if baseMcp := filepath.Join(base, ".mcp.json"); pathExists(baseMcp) {
			_ = copyFile(baseMcp, rootMcp)
		}
	}
	// Auth/settings: bidirectional newer-wins sync with the base, so a re-login or
	// another project's refreshed token flows in, and this project's in-session
	// token refresh flows back out to the base for the others.
	for _, name := range authSyncFiles {
		syncAuthFile(base, root, name)
	}
	return root, nil
}

// ConfigDir returns the per-project config root for repoRoot and whether
// isolation is active (the base ~/.claude+ exists). Callers that must locate
// Claude's config-relative files (the transcript tailer, memory dir) use this so
// they read from the same root claude+ launches Claude against.
func ConfigDir(repoRoot string) (string, bool) {
	base, err := plusDir()
	if err != nil {
		return "", false
	}
	if _, statErr := os.Stat(base); statErr != nil {
		return "", false
	}
	return filepath.Join(base, "roots", projectSlug(repoRoot)), true
}

// syncAuthFile makes the base and project-root copies of a single auth file
// converge by copying whichever is newer onto the other (last-writer-wins by
// mtime). If only one side exists it is propagated to the other; if neither
// exists it is a no-op. Best-effort: copy errors are swallowed so a transient
// failure never blocks a spawn.
func syncAuthFile(base, root, name string) {
	bp := filepath.Join(base, name)
	rp := filepath.Join(root, name)
	bt, bok := fileMTime(bp)
	rt, rok := fileMTime(rp)
	switch {
	case bok && !rok:
		_ = copyFile(bp, rp)
	case rok && !bok:
		_ = copyFile(rp, bp)
	case bok && rok:
		if bt.After(rt) {
			_ = copyFile(bp, rp)
		} else if rt.After(bt) {
			_ = copyFile(rp, bp)
		}
	}
}

// fileMTime returns a file's modification time and whether it exists.
func fileMTime(p string) (mtime time.Time, ok bool) {
	fi, err := os.Stat(p)
	if err != nil {
		return time.Time{}, false
	}
	return fi.ModTime(), true
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
