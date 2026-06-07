package topic

import (
	"os"
	"path/filepath"
	"strings"
)

// Limiter is the daemon-wide concurrency cap on judge spawns: a token bucket of N
// permits shared across every tab, so a burst of simultaneous Stops can never
// fan out into an unbounded number of headless model calls (D3). Acquire is
// non-blocking — a turn that can't get a permit simply skips the judge this time
// rather than queueing (the next firing turn re-attempts), so the gate never
// stalls the capture loop.
type Limiter struct {
	ch chan struct{}
}

// NewLimiter builds a limiter with n permits (the design fixes n=2).
func NewLimiter(n int) *Limiter {
	if n < 1 {
		n = 1
	}
	return &Limiter{ch: make(chan struct{}, n)}
}

// TryAcquire takes a permit without blocking, returning false when all permits
// are in use.
func (l *Limiter) TryAcquire() bool {
	if l == nil {
		return true
	}
	select {
	case l.ch <- struct{}{}:
		return true
	default:
		return false
	}
}

// Release returns a permit. Safe to call only after a successful TryAcquire.
func (l *Limiter) Release() {
	if l == nil {
		return
	}
	select {
	case <-l.ch:
	default:
	}
}

// sliceCaps bound the transcript slice handed to the judge so a giant turn never
// blows up the prompt. The slice is position-aware: the latest user prompt
// (head) plus the tail of the last assistant turn.
const (
	maxPromptChars    = 1500
	maxAssistantChars = 3000
)

// BuildTranscriptSlice assembles the bounded, position-aware transcript slice for
// the judge from a harvested turn: the latest user prompt (the turn's opener)
// followed by the tail of the last assistant reply. Each side is clipped
// independently so neither can crowd out the other.
func BuildTranscriptSlice(latestPrompt, assistantTail string) string {
	var b strings.Builder
	if p := strings.TrimSpace(latestPrompt); p != "" {
		b.WriteString("user: ")
		b.WriteString(clipTail(p, maxPromptChars))
		b.WriteString("\n")
	}
	if a := strings.TrimSpace(assistantTail); a != "" {
		b.WriteString("assistant: ")
		// Tail (not head) of the assistant turn: the resolution/conclusion is the
		// signal, and it sits at the end.
		b.WriteString(clipTail(a, maxAssistantChars))
	}
	return strings.TrimSpace(b.String())
}

// clipTail keeps the last n runes of s (rune-safe) — the tail carries the turn's
// conclusion. A short string is returned whole.
func clipTail(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[len(r)-n:])
}

// NearestDoc resolves the nearest feature/PRD doc that plausibly covers a touched
// file, returning the doc's RELATIVE path (the docRef carried on a doc learning)
// and its CONTENTS (the judge input). It returns ("","") — treat as no doc — when
// nothing plausibly covers a touched file, so contradicts_doc biases to false
// (R6: everything works with zero docs).
//
// Heuristic: a touched file's base-name stem (sans extension) appearing in a
// docs/plans/features/* filename is "plausible coverage"; otherwise we fall back
// to docs/PRD.html ONLY if it exists, since a generic PRD plausibly covers the
// project broadly. Lexical-only and intentionally conservative — the judge makes
// the final contradiction call.
func NearestDoc(repoRoot string, filesTouched []string) (docRef, contents string) {
	featDir := filepath.Join(repoRoot, "docs", "plans", "features")
	entries, _ := os.ReadDir(featDir)
	for _, f := range filesTouched {
		stem := docStem(f)
		if stem == "" {
			continue
		}
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			name := strings.ToLower(e.Name())
			if strings.Contains(name, stem) {
				rel := filepath.ToSlash(filepath.Join("docs", "plans", "features", e.Name()))
				if b, err := os.ReadFile(filepath.Join(featDir, e.Name())); err == nil {
					return rel, string(b)
				}
			}
		}
	}
	// Fallback: a project PRD that exists plausibly covers the work broadly.
	prd := filepath.Join(repoRoot, "docs", "PRD.html")
	if b, err := os.ReadFile(prd); err == nil {
		return "docs/PRD.html", string(b)
	}
	return "", ""
}

// docStem reduces a touched file path to a lowercase, alpha-normalized stem (the
// base name without extension, non-alnum runs collapsed) used to match it against
// a feature-doc filename. Returns "" for a too-short/empty stem (no useful match).
func docStem(p string) string {
	base := filepath.Base(filepath.ToSlash(p))
	if i := strings.LastIndexByte(base, '.'); i > 0 {
		base = base[:i]
	}
	var b strings.Builder
	for _, r := range strings.ToLower(base) {
		if r >= 'a' && r <= 'z' || r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	s := b.String()
	if len(s) < 4 {
		return ""
	}
	return s
}
