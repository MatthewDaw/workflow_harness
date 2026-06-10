package config

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// ProjectIDFor derives the HQ project id for a repo. It MUST match how HQ slugs a
// connected project — from the repo's "owner/repo" (the value carried on
// session.start's `repo` field via RepoNameFor), lowercased with every run of
// non-alphanumeric characters collapsed to a single dash.
//
// It previously slugged only the repo FOLDER name (e.g. `fractions-tutorial`),
// which diverged from HQ's owner/repo id (`matthewdaw-fractions-tutorial`): the
// per-project sync then GET /projects/{wrong-id} resolved to an empty project, so
// the tight-mirror prune deleted EVERY catalog skill — including the hq-* control-
// plane skills — even though the real project had them enabled. Slugging the same
// owner/repo HQ does keeps the two in lockstep. (RepoNameFor falls back to the
// folder name when there is no git remote, preserving the old id for those.)
//
// This is the SINGLE derivation of the HQ project id in the codebase: the daemon
// runtime, the per-session launch path, and the config-dir manifest all call it.
func ProjectIDFor(repoRoot string) string {
	base := strings.ToLower(RepoNameFor(repoRoot))
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
	s := strings.Trim(b.String(), "-")
	if s == "" {
		return "project"
	}
	return s
}

// gitRemoteURL returns the origin remote URL for repoRoot, or ("", false) if
// there is no git repo / no origin remote / git is unavailable. It is a package
// var so tests can stub it without invoking real git.
var gitRemoteURL = func(repoRoot string) (string, bool) {
	cmd := exec.Command("git", "-C", repoRoot, "remote", "get-url", "origin")
	out, err := cmd.Output()
	if err != nil {
		return "", false
	}
	url := strings.TrimSpace(string(out))
	if url == "" {
		return "", false
	}
	return url, true
}

// RepoNameFor derives a human-readable repo display name for repoRoot. It
// prefers the git origin remote reduced to "owner/repo" (host and trailing
// .git stripped); when there is no usable remote it falls back to the repo
// folder's base name. This is the value carried on session.start's `repo`
// field and used by HQ as the Project's display name + repo.
func RepoNameFor(repoRoot string) string {
	if url, ok := gitRemoteURL(repoRoot); ok {
		if name := ownerRepoFromRemote(url); name != "" {
			return name
		}
	}
	return filepath.Base(repoRoot)
}

// ownerRepoFromRemote reduces a git remote URL to "owner/repo", stripping the
// scheme/host and any trailing ".git". It handles both SCP-style
// ("git@github.com:owner/repo.git") and URL-style
// ("https://github.com/owner/repo.git") remotes. Returns "" if it can't
// extract a sensible owner/repo pair.
func ownerRepoFromRemote(url string) string {
	s := strings.TrimSpace(url)
	s = strings.TrimSuffix(s, "/")
	// Drop a trailing ".git" suffix.
	s = strings.TrimSuffix(s, ".git")
	if s == "" {
		return ""
	}
	// Normalize separators: SCP form uses ':' after the host; URL form uses '/'.
	// Strip an explicit scheme first (e.g. "https://", "ssh://", "git://").
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
		// Strip optional "user@" before host.
		if at := strings.Index(s, "@"); at >= 0 {
			s = s[at+1:]
		}
	} else if at := strings.Index(s, "@"); at >= 0 {
		// SCP-style "git@host:owner/repo".
		s = s[at+1:]
	}
	// At this point s is "host[:/]owner/repo...". Replace the first ':' with '/'
	// so the path splits uniformly.
	s = strings.Replace(s, ":", "/", 1)
	parts := strings.Split(s, "/")
	// Drop empty segments (e.g. from leading host// artifacts).
	clean := parts[:0]
	for _, p := range parts {
		if p != "" {
			clean = append(clean, p)
		}
	}
	if len(clean) < 3 {
		// Need at least host + owner + repo; otherwise no meaningful owner/repo.
		if len(clean) == 2 {
			// No host segment (e.g. already "owner/repo"): take it as-is.
			return clean[0] + "/" + clean[1]
		}
		return ""
	}
	// Last two segments are owner/repo; everything before is host/path.
	owner := clean[len(clean)-2]
	repo := clean[len(clean)-1]
	return owner + "/" + repo
}

// RepoRootFor walks up from dir to the nearest directory containing a .git
// directory and returns it, falling back to dir itself when none is found
// (every directory can host a daemon). It is the single repo-root resolution
// shared by the CLI and desktop entry points.
func RepoRootFor(dir string) string {
	d := dir
	for {
		if fi, err := os.Stat(filepath.Join(d, ".git")); err == nil && fi.IsDir() {
			return d
		}
		parent := filepath.Dir(d)
		if parent == d {
			return dir
		}
		d = parent
	}
}

// Credentials is the parsed ~/.claude-plus/credentials file written by
// `claude+ login`: line 1 the WebSocket URL, line 2 the device token, line 3
// the REST API base (optional in older files). Absent lines parse as "".
type Credentials struct {
	WSURL   string
	Token   string
	APIBase string
}

// LoadCredentials reads and parses ~/.claude-plus/credentials. ok is false when
// the home dir cannot be resolved or the file is missing/unreadable; callers
// still check the individual fields they need for emptiness. It is the single
// reader of the credentials file shared by the CLI entry points and APIBase.
func LoadCredentials() (Credentials, bool) {
	home, err := os.UserHomeDir()
	if err != nil {
		return Credentials{}, false
	}
	b, err := os.ReadFile(filepath.Join(home, ".claude-plus", "credentials"))
	if err != nil {
		return Credentials{}, false
	}
	lines := splitCredLines(string(b))
	var c Credentials
	if len(lines) >= 1 {
		c.WSURL = strings.TrimSpace(lines[0])
	}
	if len(lines) >= 2 {
		c.Token = strings.TrimSpace(lines[1])
	}
	if len(lines) >= 3 {
		c.APIBase = strings.TrimSpace(lines[2])
	}
	return c, true
}

// APIBase resolves HQ's REST base URL (distinct from the WebSocket URL): the
// CLAUDE_PLUS_API_URL env var, or the third credentials line. Returns ok=false
// when neither is configured. It is the single resolver shared by the daemon
// runtime and the per-session launch path so they never disagree about where
// HQ lives.
func APIBase() (string, bool) {
	if base := os.Getenv("CLAUDE_PLUS_API_URL"); base != "" {
		return base, true
	}
	c, ok := LoadCredentials()
	if !ok || c.APIBase == "" {
		return "", false
	}
	return c.APIBase, true
}

// splitCredLines splits credentials-file text into CR-trimmed lines, mirroring
// the daemon's splitLines so APIBase parses the file identically. Kept local to
// the config package so it does not import the daemon.
func splitCredLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, trimCredCR(s[start:i]))
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, trimCredCR(s[start:]))
	}
	return out
}

func trimCredCR(s string) string {
	if len(s) > 0 && s[len(s)-1] == '\r' {
		return s[:len(s)-1]
	}
	return s
}
