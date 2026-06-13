package capture

import (
	"encoding/json"
	"os/exec"
	"regexp"
	"strings"
	"sync"
)

// BranchSessionLink is the payload written to DynamoDB at push time (U7).
// It maps a (repo, branch) pair to the session that authored the branch and
// the turn-range slice relevant for distillation.
//
// Key: BSLINK#<ownerRepo>#<branch>  (written by writeBranchSessionLink)
// TTL: 90 days (the EVT# records it is derived from expire, so pre-distill)
type BranchSessionLink struct {
	// Repo is the owner/repo string, e.g. "acme/backend".
	Repo string
	// Branch is the git branch name, e.g. "feat/my-feature".
	Branch string
	// SessionID is the stable tab/session id (PinnedSessionID) of the Claude
	// session that ran the git push.  Uses PinnedSessionID to remain stable
	// across /resume.
	SessionID string
	// TurnStart / TurnEnd are the turn index boundaries of the relevant slice
	// within the session.  Set from the session's LEARN# turn ledger at push
	// time.  Zero values are used when the turn range is unknown.
	TurnStart int
	TurnEnd   int
	// DistilledContext is the eagerly-distilled, scrubbed summary of the turn
	// slice — stored at push time so U1 never needs to re-read EVT# records at
	// merge time (avoids the 90-day TTL expiry trap).
	DistilledContext string
}

// IsBashGitPush reports whether a PostToolUse hook event represents a
// successful "git push" Bash invocation.  Returns true only when:
//   - ToolName is "Bash" (case-sensitive: that is Claude Code's tool name), AND
//   - ToolInput contains a command that includes "git push" (not a dry-run, not
//     a push to a detached HEAD — caller must validate the output for errors).
//
// This is intentionally a cheap syntactic check; the caller (ingestHook) also
// validates the exit-code / failure by inspecting ToolOutput (the hook carries
// the tool's output on PostToolUse).
func IsBashGitPush(h HookEvent) bool {
	if h.ToolName != "Bash" {
		return false
	}
	cmd := bashCommand(h.ToolInput)
	return containsGitPush(cmd)
}

// ParsePushBranch extracts the branch name from a "git push" command string.
// It handles the common forms:
//
//	git push                         → "" (let caller use HEAD)
//	git push origin                  → ""
//	git push origin feat/foo         → "feat/foo"
//	git push origin HEAD:feat/foo    → "feat/foo"
//	git push --set-upstream origin … → same rules after flags
//	git push -u origin feat/foo      → "feat/foo"
//
// Returns "" when the branch cannot be determined from the command alone (the
// caller falls back to reading the local HEAD ref).
func ParsePushBranch(cmd string) string {
	// Normalise: collapse whitespace, remove common flags.
	words := tokenise(cmd)
	// Strip "git" and "push".
	if len(words) == 0 || words[0] != "git" {
		return ""
	}
	words = words[1:]
	if len(words) == 0 || words[0] != "push" {
		return ""
	}
	words = words[1:]

	// Walk remaining words, skipping flags and the remote name.
	skippedRemote := false
	for _, w := range words {
		if strings.HasPrefix(w, "-") {
			// skip flag (and its value is merged in the tokeniser — see below)
			continue
		}
		if !skippedRemote {
			skippedRemote = true // first non-flag word is the remote
			continue
		}
		// Second non-flag word is the refspec: "branch" or "local:remote".
		// For "HEAD:branch" take the remote half; otherwise take the whole word.
		if colon := strings.Index(w, ":"); colon >= 0 {
			return w[colon+1:]
		}
		return w
	}
	return ""
}

// ParsePushFailure reports whether a PostToolUse tool_output string indicates
// the push failed.  Claude Code's PostToolUse hook carries the tool's output
// in a JSON field; a non-zero exit code or stderr containing "error"/"fatal"
// is treated as failure.  Best-effort: false (i.e. "treat as success") when
// the output is unparseable.
func ParsePushFailure(toolOutput string) bool {
	if toolOutput == "" {
		return false
	}
	// ToolInput on PostToolUse is the JSON input; ToolOutput is carried
	// separately.  We receive it as the raw JSON string the hook emits.
	// Check for common git error patterns in the raw text.
	lower := strings.ToLower(toolOutput)
	for _, marker := range []string{"error:", "fatal:", "rejected", "failed to push"} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

// IsDetachedHead reports whether a HEAD-ref string indicates detached HEAD
// state (starts with "HEAD detached" or is a raw SHA-like string with no
// "refs/heads/" prefix).
func IsDetachedHead(headRef string) bool {
	if strings.Contains(headRef, "HEAD detached") {
		return true
	}
	// A symbolic ref is "refs/heads/<branch>"; a bare SHA has no slash segments.
	// Treat a 40-char hex string with no slashes as detached.
	if len(headRef) == 40 && isHex(headRef) {
		return true
	}
	return false
}

// ScrubSecrets removes known secret patterns from text before it is stored as
// distilledContext.  It applies the six patterns from topic/fold.go:
//   1. AWS AKIA keys
//   2. key/secret/token = VALUE assignments
//   3. Bearer tokens
//   4. JWT strings
//   5. PEM headers
//   6. GitHub PATs (ghp_…)
func ScrubSecrets(s string) string {
	for _, re := range scrubPatterns {
		s = re.ReplaceAllString(s, "[REDACTED]")
	}
	return s
}

// ----------------------------------------------------------------------------
// internal helpers
// ----------------------------------------------------------------------------

var scrubPatterns = []*regexp.Regexp{
	// AWS AKIA access-key ids.
	regexp.MustCompile(`AKIA[0-9A-Z]{16}`),
	// key/secret/token assignments (env-var or config form).
	regexp.MustCompile(`(?i)(key|secret|token|password)\s*[=:]\s*\S+`),
	// Bearer tokens in headers.
	regexp.MustCompile(`(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*`),
	// JWT strings (three base64url segments separated by dots).
	regexp.MustCompile(`eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+`),
	// PEM block headers.
	regexp.MustCompile(`-----BEGIN [A-Z ]+-----`),
	// GitHub PATs.
	regexp.MustCompile(`ghp_[A-Za-z0-9]{36}`),
}

// bashCommand extracts the "command" string from a JSON tool_input object.
// Returns "" if the JSON is malformed or the field is absent.
func bashCommand(toolInput string) string {
	if toolInput == "" {
		return ""
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal([]byte(toolInput), &obj); err != nil {
		return ""
	}
	raw, ok := obj["command"]
	if !ok {
		return ""
	}
	var cmd string
	if err := json.Unmarshal(raw, &cmd); err != nil {
		return ""
	}
	return cmd
}

// containsGitPush reports whether a shell command string contains a
// "git push" invocation (not just the words "git" and "push" in unrelated
// positions).
func containsGitPush(cmd string) bool {
	// Normalise multi-line / semicolon-separated commands.
	for _, segment := range splitShell(cmd) {
		words := tokenise(segment)
		for i := 0; i+1 < len(words); i++ {
			if words[i] == "git" && words[i+1] == "push" {
				return true
			}
		}
	}
	return false
}

// splitShell splits a shell command on common separators (&&, ||, ;, newline)
// so we can check each sub-command independently.
var shellSepRe = regexp.MustCompile(`[;&\n]+|&&|\|\|`)

func splitShell(cmd string) []string {
	return shellSepRe.Split(cmd, -1)
}

// tokenise splits a shell line into words, collapsing whitespace and
// treating quoted strings as single tokens (simplified — no escape handling
// needed for our detection use-case).
func tokenise(line string) []string {
	line = strings.TrimSpace(line)
	if line == "" {
		return nil
	}
	var out []string
	var cur strings.Builder
	inQ := false
	qChar := byte(0)
	for i := 0; i < len(line); i++ {
		c := line[i]
		if inQ {
			if c == qChar {
				inQ = false
			} else {
				cur.WriteByte(c)
			}
			continue
		}
		if c == '\'' || c == '"' {
			inQ = true
			qChar = c
			continue
		}
		if c == ' ' || c == '\t' {
			if cur.Len() > 0 {
				out = append(out, cur.String())
				cur.Reset()
			}
			continue
		}
		cur.WriteByte(c)
	}
	if cur.Len() > 0 {
		out = append(out, cur.String())
	}
	return out
}

func isHex(s string) bool {
	for _, c := range s {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return true
}

// ----------------------------------------------------------------------------
// BranchSessionStore — in-process write backend for branch→session links (U7)
// ----------------------------------------------------------------------------

// BranchSessionStore is a concurrency-safe in-process store for
// BranchSessionLink records.  It is the default write target used by the
// Runtime's branch-session hook when no external persistence (e.g. DynamoDB)
// is configured.  Tests can read from it directly via Get / All to assert that
// links were written.  A nil store is safe: Write is a no-op.
type BranchSessionStore struct {
	mu    sync.RWMutex
	links map[string]*BranchSessionLink // key: repo+"#"+branch
}

// NewBranchSessionStore returns a ready-to-use in-process store.
func NewBranchSessionStore() *BranchSessionStore {
	return &BranchSessionStore{links: map[string]*BranchSessionLink{}}
}

// Write persists a link (last-writer-wins per branch).
func (s *BranchSessionStore) Write(link BranchSessionLink) {
	if s == nil {
		return
	}
	key := link.Repo + "#" + link.Branch
	s.mu.Lock()
	s.links[key] = &link
	s.mu.Unlock()
}

// Get returns the link for (repo, branch), or ok=false if absent.
func (s *BranchSessionStore) Get(repo, branch string) (BranchSessionLink, bool) {
	if s == nil {
		return BranchSessionLink{}, false
	}
	key := repo + "#" + branch
	s.mu.RLock()
	defer s.mu.RUnlock()
	v, ok := s.links[key]
	if !ok {
		return BranchSessionLink{}, false
	}
	return *v, true
}

// All returns a snapshot of every stored link.
func (s *BranchSessionStore) All() []BranchSessionLink {
	if s == nil {
		return nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]BranchSessionLink, 0, len(s.links))
	for _, v := range s.links {
		out = append(out, *v)
	}
	return out
}

// ----------------------------------------------------------------------------
// Git HEAD resolution
// ----------------------------------------------------------------------------

// resolveGitHEAD is the default implementation of ResolveGitHEADForTest.
// It invokes real git; the exported var lets tests inject a stub without a git
// repo.
func defaultResolveGitHEAD(repoRoot string) string {
	out, err := exec.Command("git", "-C", repoRoot, "symbolic-ref", "--short", "HEAD").Output()
	if err != nil {
		// Detached HEAD or git unavailable — fall back to the long ref so the
		// caller can detect detached-head state via IsDetachedHead.
		out2, err2 := exec.Command("git", "-C", repoRoot, "rev-parse", "HEAD").Output()
		if err2 != nil {
			return ""
		}
		return strings.TrimSpace(string(out2))
	}
	return strings.TrimSpace(string(out))
}

// ResolveGitHEADForTest is a package-level seam so tests (inside and outside
// this package) can inject a stub HEAD resolver without invoking real git.
// Production code calls this via the closure in BuildBranchSessionLink.
var ResolveGitHEADForTest = defaultResolveGitHEAD

// RepoOwnerName returns a best-effort "owner/repo" string derived from the git
// remote URL for the given repo root.  Falls back to the directory basename when
// no remote is configured or git is unavailable.  It is a package var for test
// injection.
var RepoOwnerNameForTest = func(repoRoot string) string {
	out, err := exec.Command("git", "-C", repoRoot, "remote", "get-url", "origin").Output()
	if err == nil {
		url := strings.TrimSpace(string(out))
		// Strip trailing ".git".
		url = strings.TrimSuffix(url, ".git")
		// Handle SSH form: git@github.com:owner/repo
		if idx := strings.LastIndex(url, ":"); idx >= 0 && !strings.HasPrefix(url, "http") {
			url = url[idx+1:]
		}
		// Handle HTTPS form: strip scheme + host prefix.
		if idx := strings.Index(url, "github.com/"); idx >= 0 {
			url = url[idx+11:]
		}
		if strings.Contains(url, "/") {
			return url
		}
	}
	// Fall back to directory basename.
	parts := strings.FieldsFunc(repoRoot, func(r rune) bool { return r == '/' || r == '\\' })
	if len(parts) > 0 {
		return parts[len(parts)-1]
	}
	return repoRoot
}

// BuildBranchSessionLink constructs a BranchSessionLink for a successful git
// push.  It resolves the branch from the push command (falling back to HEAD
// when the command omits it), computes a minimal turn-range from turnStart and
// turnEnd (zero values are used when the ledger is unavailable), and stores the
// eagerly-distilled, scrubbed command context as DistilledContext.
//
// Returns ok=false when the push should NOT produce a link:
//   - toolOutput indicates a push failure (ParsePushFailure).
//   - the resolved HEAD ref indicates detached-HEAD state (IsDetachedHead).
func BuildBranchSessionLink(
	sessionID, repoRoot, toolInput, toolOutput string,
	turnStart, turnEnd int,
) (BranchSessionLink, bool) {
	// Reject failed pushes.
	if ParsePushFailure(toolOutput) {
		return BranchSessionLink{}, false
	}

	// Resolve branch from the push command or fall back to HEAD.
	cmd := bashCommand(toolInput)
	branch := ParsePushBranch(cmd)
	if branch == "" {
		branch = ResolveGitHEADForTest(repoRoot)
	}
	if branch == "" || IsDetachedHead(branch) {
		return BranchSessionLink{}, false
	}

	// Eager distillation: build a concise, scrubbed context string from the
	// push command.  EVT# records have a 90-day TTL; storing this at push time
	// means U1 never needs to re-read EVT# at merge time.
	distilled := ScrubSecrets(cmd)

	repo := RepoOwnerNameForTest(repoRoot)

	return BranchSessionLink{
		Repo:             repo,
		Branch:           branch,
		SessionID:        sessionID,
		TurnStart:        turnStart,
		TurnEnd:          turnEnd,
		DistilledContext: distilled,
	}, true
}
