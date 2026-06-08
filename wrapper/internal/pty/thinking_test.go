package pty

import (
	"strings"
	"testing"
)

// envValue returns the value of the LAST `KEY=value` entry for key in env, or ""
// — matching how a child process resolves a duplicated env var (last wins).
func envValue(env []string, key string) string {
	val := ""
	prefix := key + "="
	for _, e := range env {
		if strings.HasPrefix(e, prefix) {
			val = e[len(prefix):]
		}
	}
	return val
}

// TestDefaultSpawnDefaultsThinkingBudget proves the wrapper raises the inner
// claude's extended-thinking ceiling to 32000 by default, so the HumanLayer ACE
// skills' "ultrathink" magic word can spend its full budget (mirrors HumanLayer's
// .claude/settings.json MAX_THINKING_TOKENS=32000). The imported commands become
// skills, which carry no model/thinking pin of their own, so this is where that
// learning is captured.
func TestDefaultSpawnDefaultsThinkingBudget(t *testing.T) {
	t.Setenv("MAX_THINKING_TOKENS", "")
	spec := DefaultSpawn("/repo", "id")
	if got := envValue(spec.Env, "MAX_THINKING_TOKENS"); got != "32000" {
		t.Fatalf("DefaultSpawn must default MAX_THINKING_TOKENS=32000; got %q", got)
	}
}

// TestDefaultSpawnHonorsExistingThinkingBudget proves the default is no-clobber:
// a developer who already exported MAX_THINKING_TOKENS keeps their value.
func TestDefaultSpawnHonorsExistingThinkingBudget(t *testing.T) {
	t.Setenv("MAX_THINKING_TOKENS", "8000")
	spec := DefaultSpawn("/repo", "id")
	if got := envValue(spec.Env, "MAX_THINKING_TOKENS"); got != "8000" {
		t.Fatalf("DefaultSpawn must not clobber an existing MAX_THINKING_TOKENS; got %q", got)
	}
}
