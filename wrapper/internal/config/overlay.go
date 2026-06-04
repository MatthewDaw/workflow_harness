package config

import (
	"io"
	"os"
	"path/filepath"
)

// The isolated config root for claude+ inner sessions (U21).
//
// claude+ launches its inner Claude with CLAUDE_CONFIG_DIR pointed at a
// per-session directory under ~/.claude+, so the skills/agents bundled with the
// product and the session history claude+ generates never land in the user's
// personal ~/.claude. Because CLAUDE_CONFIG_DIR is set only on the child claude+
// spawns, a normal `claude` (claude+ not running, or an unrelated session) reads
// ~/.claude and never sees this root — the isolation is "in place" only for the
// specific inner Claude claude+ launches.
//
// Layout:
//
//	~/.claude+/
//	  skills/        product-bundled skills (stable; shared across sessions)
//	  agents/        product-bundled agents
//	  run/<id>/      a per-session config root (ephemeral; removed on close)
//	    skills   -> ../../skills      (link, claude+ only)
//	    agents   -> ../../agents
//	    .credentials.json -> ~/.claude/.credentials.json   (symlink; token refresh writes through)
//	    settings.json     (copied snapshot — claude+ can't edit the real file)
//	    .mcp.json         (copied snapshot)

// sharedLinkFiles are symlinked from the user's ~/.claude so writes pass through
// to the real shared file. The credential store must persist refreshed tokens
// back to the user's actual login.
var sharedLinkFiles = []string{".credentials.json"}

// sharedCopyFiles are snapshot-copied from ~/.claude into each session root, so
// an in-claude+ change can't edit the user's real config: settings + MCP servers.
var sharedCopyFiles = []string{"settings.json", ".mcp.json"}

// plusDir resolves ~/.claude+.
func plusDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".claude+"), nil
}

// BuildSessionConfigDir builds an isolated, session-scoped CLAUDE_CONFIG_DIR for
// one inner claude and returns it plus a cleanup func that removes the per-session
// directory (the stable ~/.claude+/skills + agents survive). It never writes into
// ~/.claude. A logged-out user (no ~/.claude/.credentials.json) still gets a
// usable root — the credential link is simply omitted.
func BuildSessionConfigDir(sessionID string) (dir string, cleanup func(), err error) {
	plus, err := plusDir()
	if err != nil {
		return "", nil, err
	}
	user, err := claudeDir()
	if err != nil {
		return "", nil, err
	}

	dir = filepath.Join(plus, "run", sessionID)
	// Start each session from a clean root so a crashed prior run can't leak.
	_ = os.RemoveAll(dir)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", nil, err
	}
	cleanup = func() { _ = os.RemoveAll(dir) }

	// Link the stable product skills/agents source into the session root. The
	// source dirs are ensured so a fresh install (nothing seeded yet) still
	// yields a valid, empty registry rather than a dangling link.
	for _, sub := range []string{"skills", "agents"} {
		src := filepath.Join(plus, sub)
		if mkErr := os.MkdirAll(src, 0o755); mkErr != nil {
			continue
		}
		linkDirOrCopy(filepath.Join(dir, sub), src)
	}

	// Shared login: symlink so refreshed tokens persist to the real file.
	for _, name := range sharedLinkFiles {
		src := filepath.Join(user, name)
		if !pathExists(src) {
			continue
		}
		dst := filepath.Join(dir, name)
		if linkErr := os.Symlink(src, dst); linkErr != nil {
			// Windows without Developer Mode / elevation can't symlink; fall back
			// to a copy. Degraded: a token refreshed inside claude+ won't persist
			// back to ~/.claude, but the session still launches authenticated.
			_ = copyFile(src, dst)
		}
	}

	// Shared settings + MCP: copy a read-only snapshot.
	for _, name := range sharedCopyFiles {
		src := filepath.Join(user, name)
		if !pathExists(src) {
			continue
		}
		_ = copyFile(src, filepath.Join(dir, name))
	}

	return dir, cleanup, nil
}

// linkDirOrCopy symlinks dst -> src, falling back to a recursive copy when the
// platform refuses symlinks (Windows without Developer Mode).
func linkDirOrCopy(dst, src string) {
	if err := os.Symlink(src, dst); err == nil {
		return
	}
	_ = copyTree(dst, src)
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

func copyTree(dst, src string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		return copyFile(path, target)
	})
}
