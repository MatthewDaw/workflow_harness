package config

import (
	"bytes"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// The isolated config root for claude+ inner sessions, PER-PROJECT.
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
//	  .credentials.json .claude.json settings.json
//	                                  seeded once from ~/.claude; where re-login lands
//	  roots/<projectSlug>/            PER-PROJECT root = a session's CLAUDE_CONFIG_DIR
//	    skills/ agents/               scoped to THIS project's enabled set only
//	    .claude.json                  mcpServers merged in by ApplyPulled
//	    projects/                     this repo's transcripts/memory
//	    .credentials.json settings.json
//	                                  .credentials.json kept in sync with the BASE
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
// logging another out. `.claude.json` is NOT auth-synced: it is seeded into the
// root ONCE (so the project's pulled MCP servers, merged into its mcpServers by
// ApplyPulled, persist) and then owned per-project.

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
	// NOTE: ~/.claude/.mcp.json is no longer seeded — Claude reads MCP servers from
	// <root>/.claude.json mcpServers, not a separate .mcp.json file,
	// and the personal .claude.json (seeded above) already carries them.
}

// authSyncFiles are the per-project root files kept convergent with the BASE on
// every spawn (newer-wins, both directions). Only true auth/identity files belong
// here: a credential the inner Claude refreshes in one project must reach the
// others. `.claude.json` and `settings.json` are NOT here — they are seeded once
// and then owned per-project (see seedOnceFiles), so a project's pulled MCP
// servers (merged into <root>/.claude.json mcpServers) and its
// own permissions/model/hooks settings never leak into another project.
var authSyncFiles = []string{".credentials.json"}

// seedOnceFiles are copied from the BASE into a project root exactly once (only
// when absent), then owned per-project. `.claude.json` carries the user's personal
// MCP servers (under mcpServers) plus per-project Claude state, onto which
// ApplyPulled merges this project's HQ MCP servers — so a later base copy must
// never clobber them. `settings.json` carries per-project permissions/model plus
// the managed hooks block (installed into the root by the daemon), which must not
// be whole-file synced across projects.
var seedOnceFiles = []string{".claude.json", "settings.json"}

// plusDir resolves the BASE ~/.claude+ (canonical auth + the parent of every
// per-project root). Kept as the package's single home-relative anchor.
func plusDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude+"), nil
}

// canonRepoRoot normalizes a repo path to a stable identity so the SAME physical
// repo always maps to the SAME per-project root regardless of how it was spelled
// at launch. It resolves to an absolute, symlink-free path, and on Windows folds
// case + separators (the FS is case-insensitive, so C:\Users\Me\repo and
// c:\users\me\repo are the same directory and must not spawn two roots). Used for
// BOTH the readable slug prefix and the identity hash so they never diverge.
func canonRepoRoot(repoRoot string) string {
	r := repoRoot
	if abs, err := filepath.Abs(r); err == nil {
		r = abs
	}
	if resolved, err := filepath.EvalSymlinks(r); err == nil && resolved != "" {
		r = resolved
	}
	if runtime.GOOS == "windows" {
		r = strings.ToLower(filepath.Clean(r))
	}
	return r
}

// projectSlug derives a stable, readable, collision-free directory name for a
// repo's per-project root: the canonicalized base folder name (slugified, capped)
// plus an 8-hex suffix of the canonical path's SHA-1, so two repos that share a
// folder name in different locations never collide AND the same repo launched
// under a different casing/spelling resolves to one root. The prefix is length-
// capped to keep the nested transcript path well under Windows MAX_PATH. Self-
// contained in this package (no import of the daemon/capture slug helpers).
func projectSlug(repoRoot string) string {
	canon := canonRepoRoot(repoRoot)
	base := strings.ToLower(filepath.Base(canon))
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
	if len(slug) > 32 {
		slug = strings.Trim(slug[:32], "-")
	}
	sum := sha1.Sum([]byte(canon))
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
// seeding its AUTH/IDENTITY files on first use (copied once from ~/.claude, only
// when absent, so claude+'s own evolving state is never clobbered). It is
// idempotent — safe to call on every session start. Used directly by InstallHooks
// (hooks live in the base settings.json and propagate to each project root via the
// auth sync).
//
// On this machine, claude+ skills/agents are ALWAYS scoped to a per-project root
// (roots/<slug>); there is no global claude+ skill set. So EnsureBaseDir actively
// DELETES any legacy base-level `skills`/`agents` (left over from the
// pre-per-project-root design) and never recreates them — nothing reads them
// (ReadLocal reads ~/.claude ∪ the per-project root only).
func EnsureBaseDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	base := filepath.Join(home, ".claude+")
	if err := os.MkdirAll(base, 0o755); err != nil {
		return "", err
	}
	// Remove any vestigial GLOBAL claude+ skills/agents. Best-effort and idempotent
	// (a no-op once gone); never recreated.
	for _, sub := range []string{"skills", "agents"} {
		_ = os.RemoveAll(filepath.Join(base, sub))
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
	// `.claude.json` and `settings.json` are seeded ONCE from the base (the
	// personal MCP servers / personal settings) so this project can own them:
	// pulled HQ MCP servers and the per-project hooks block merged in afterwards
	// are never clobbered on later spawns.
	for _, name := range seedOnceFiles {
		rp := filepath.Join(root, name)
		if pathExists(rp) {
			continue
		}
		if bp := filepath.Join(base, name); pathExists(bp) {
			_ = copyFile(bp, rp)
		}
	}
	// Auth: bidirectional newer-wins sync with the base, so a re-login or another
	// project's refreshed token flows in, and this project's in-session token
	// refresh flows back out to the base for the others.
	for _, name := range authSyncFiles {
		syncAuthFile(base, root, name)
	}
	// Drop a best-effort manifest recording this project's HQ identity (the single
	// ProjectIDFor derivation, its repo display name, and the resolved HQ API base)
	// so tooling and the launched session can read the id off disk without
	// re-deriving it. Write errors are IGNORED — a manifest is never required to
	// launch a session.
	apiBase, _ := APIBase()
	manifest := struct {
		ProjectID string `json:"projectId"`
		Repo      string `json:"repo"`
		APIBase   string `json:"apiBase"`
	}{
		ProjectID: ProjectIDFor(repoRoot),
		Repo:      RepoNameFor(repoRoot),
		APIBase:   apiBase,
	}
	if data, err := json.Marshal(manifest); err == nil {
		_ = os.WriteFile(filepath.Join(root, "hq-project.json"), data, 0o644)
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
//
// Two guards keep a corrupt or coarse-mtime filesystem from destroying a good
// credential: a ZERO-LENGTH file is treated as absent (never propagated over a
// valid copy — defends against an interrupted external write), and an mtime TIE
// (coarse FS granularity / same tick) falls back to a content comparison and, if
// they differ, converges on the BASE as the canonical auth hub rather than
// leaving the two permanently divergent. copyFile preserves the source mtime, so
// a converged identical pair compares equal and this becomes a true no-op (no
// per-spawn copy churn).
func syncAuthFile(base, root, name string) {
	bp := filepath.Join(base, name)
	rp := filepath.Join(root, name)
	bs, bok := fileStat(bp)
	rs, rok := fileStat(rp)
	// A zero-length auth file is corrupt/absent for our purposes: never let it win.
	if bok && bs.size == 0 {
		bok = false
	}
	if rok && rs.size == 0 {
		rok = false
	}
	switch {
	case bok && !rok:
		_ = copyFile(bp, rp)
	case rok && !bok:
		_ = copyFile(rp, bp)
	case bok && rok:
		switch {
		case bs.mtime.After(rs.mtime):
			_ = copyFile(bp, rp)
		case rs.mtime.After(bs.mtime):
			_ = copyFile(rp, bp)
		default:
			// Equal mtime: converge on content. Prefer the base on a real difference.
			if !sameFile(bp, rp) {
				_ = copyFile(bp, rp)
			}
		}
	}
}

// statInfo carries the two facts syncAuthFile needs about a candidate file.
type statInfo struct {
	mtime time.Time
	size  int64
}

// fileStat returns a file's mtime + size and whether it exists.
func fileStat(p string) (statInfo, bool) {
	fi, err := os.Stat(p)
	if err != nil {
		return statInfo{}, false
	}
	return statInfo{mtime: fi.ModTime(), size: fi.Size()}, true
}

// sameFile reports whether two files have byte-identical contents. A read error
// on either side yields false (treat as "differs" so the caller reconciles).
func sameFile(a, b string) bool {
	ab, err := os.ReadFile(a)
	if err != nil {
		return false
	}
	bb, err := os.ReadFile(b)
	if err != nil {
		return false
	}
	return bytes.Equal(ab, bb)
}

func pathExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// copyFile copies src to dst ATOMICALLY: it streams into a temp file in the
// destination directory, fsyncs it, then os.Renames it over dst (atomic on a
// single filesystem on both POSIX and Windows). A reader therefore only ever sees
// either the old complete file or the new complete one — never a truncated or
// half-written file — and a crash mid-copy leaves dst intact (plus an orphan
// temp, which the fixed authSyncFiles/seedOnceFiles lists never pick up). The
// source mtime is preserved onto dst so a converged pair compares mtime-equal and
// syncAuthFile stops re-copying on every spawn.
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	fi, statErr := in.Stat()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(dst), ".tmp-"+filepath.Base(dst)+"-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op once the rename succeeds
	if _, err := io.Copy(tmp, in); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, dst); err != nil {
		return err
	}
	if statErr == nil {
		_ = os.Chtimes(dst, fi.ModTime(), fi.ModTime())
	}
	return nil
}
