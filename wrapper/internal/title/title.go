// Package title generates a short, human-friendly session title from the
// opening exchange of a Claude Code session. It runs a one-shot, time-boxed
// headless `claude -p` on the user's own Claude subscription to summarize the
// first user message + first assistant reply into a 2–4 word title.
//
// It is best-effort: any error, timeout, or empty result yields ok=false so the
// caller keeps the provisional first-turn slug. The headless sub-process is
// tagged with CLAUDE_PLUS_TITLE=1 so the installed hook shim skips it — the
// title call must never surface as a phantom session in the Stream / HQ.
package title

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"time"
)

// genTimeout bounds the headless call so a slow or stuck model never delays
// naming indefinitely (the provisional slug stays until then).
const genTimeout = 30 * time.Second

// maxInput clips each side of the exchange fed into the prompt, bounding token
// use — a title needs only the gist of the opening, not the whole turn.
const maxInput = 600

// claudeBin resolves the claude CLI once (PATH lookup), falling back to the bare
// name so exec surfaces a clear error if it is genuinely absent.
var claudeBin = resolveClaude()

func resolveClaude() string {
	if p, err := exec.LookPath("claude"); err == nil {
		return p
	}
	return "claude"
}

// runClaude executes the headless title prompt and returns its stdout. It is a
// package var so tests can substitute a fake without invoking the real CLI.
var runClaude = func(ctx context.Context, repoRoot, prompt string) (string, error) {
	// Haiku is the cheapest/fastest tier and ample for a few-word title.
	cmd := exec.CommandContext(ctx, claudeBin, "-p", prompt, "--model", "haiku")
	cmd.Dir = repoRoot
	// Mark this as an internal title sub-process so the installed hook shim
	// (`claude+ __hook`) skips it — see cmd/claude-plus runHook.
	cmd.Env = append(os.Environ(), "CLAUDE_PLUS_TITLE=1")
	out, err := cmd.Output()
	return string(out), err
}

// Generate summarizes the opening exchange into a short title. The returned text
// is raw (the caller slugs + disambiguates it). ok is false on any
// failure/timeout/empty input or output.
func Generate(repoRoot, userText, assistantText string) (title string, ok bool) {
	userText = clip(userText, maxInput)
	assistantText = clip(assistantText, maxInput)
	if userText == "" {
		return "", false
	}
	ctx, cancel := context.WithTimeout(context.Background(), genTimeout)
	defer cancel()
	out, err := runClaude(ctx, repoRoot, buildPrompt(userText, assistantText))
	if err != nil {
		return "", false
	}
	t := firstLine(out)
	if t == "" {
		return "", false
	}
	return t, true
}

// buildPrompt asks for a terse kebab-case title and nothing else, so the model's
// raw stdout is already close to a usable slug.
func buildPrompt(userText, assistantText string) string {
	var b strings.Builder
	b.WriteString("Write a 2-4 word title for this coding session in lowercase kebab-case ")
	b.WriteString("(words separated by hyphens, no punctuation, no quotes, no explanation). ")
	b.WriteString("Reply with ONLY the title.\n\nUser: ")
	b.WriteString(userText)
	if assistantText != "" {
		b.WriteString("\n\nAssistant: ")
		b.WriteString(assistantText)
	}
	return b.String()
}

func clip(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func firstLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}
