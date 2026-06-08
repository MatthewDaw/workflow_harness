package capture

import (
	"encoding/json"
	"os"
	"path/filepath"

	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// HookEvent is the payload Claude Code posts to our hook command. We install a
// hooks block in ~/.claude/settings.json whose command pipes the hook JSON to a
// local endpoint (the daemon socket). This gives low-latency lifecycle/status
// signals without parsing the screen (KTD3 source 3).
type HookEvent struct {
	// HookEventName is one of PreToolUse | PostToolUse | Stop | Notification |
	// UserPromptSubmit.
	HookEventName string `json:"hook_event_name"`
	SessionID     string `json:"session_id"`
	Message       string `json:"message"` // Notification text
	Prompt        string `json:"prompt"`  // UserPromptSubmit: the prompt the user typed
	// PinnedSessionID is the launch session id of the tab the daemon keys on,
	// injected by the hook shim from the child's CLAUDE_PLUS_SESSION env (see
	// cmd/claude-plus runHook). It is NOT a Claude Code field. After an in-session
	// /resume, Claude's live session_id diverges from the id we launched with, so
	// the daemon routes hooks by this stable tab id when present and falls back to
	// SessionID otherwise.
	PinnedSessionID string `json:"claude_plus_session"`
}

// settingsHook is the shape of one hook entry in settings.json.
type settingsHook struct {
	Matcher string             `json:"matcher,omitempty"`
	Hooks   []settingsHookExec `json:"hooks"`
}

type settingsHookExec struct {
	Type    string `json:"type"`    // "command"
	Command string `json:"command"` // the claude+ hook shim invocation
}

// InstallHooks merges a hooks block into the claude+ BASE config root's
// settings.json (~/.claude+/settings.json) that forwards lifecycle events to the
// daemon. The hooks are identical for every project, so they live in the base;
// each per-project root inherits them via the settings.json auth-sync in
// EnsureConfigDir, so they fire under isolation regardless of which project root
// a session is launched against. Writes are additive: existing user hooks are
// preserved; only our managed entries (identified by the shim command) are
// reconciled. Returns the settings path written.
func InstallHooks(hookCmd string) (string, error) {
	dir, err := config.EnsureBaseDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, "settings.json")

	settings := map[string]any{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, &settings) // tolerate empty/missing
	}

	hooks, _ := settings["hooks"].(map[string]any)
	if hooks == nil {
		hooks = map[string]any{}
	}

	managed := []settingsHook{{
		Hooks: []settingsHookExec{{Type: "command", Command: hookCmd}},
	}}
	for _, evt := range []string{"PreToolUse", "PostToolUse", "Stop", "Notification", "UserPromptSubmit"} {
		hooks[evt] = mergeManaged(hooks[evt], managed, hookCmd)
	}
	settings["hooks"] = hooks

	out, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(path, out, 0o644); err != nil {
		return "", err
	}
	return path, nil
}

// mergeManaged keeps existing hook entries and ensures exactly one managed entry
// (matched by command) is present — idempotent reconciliation.
func mergeManaged(existing any, managed []settingsHook, hookCmd string) []settingsHook {
	var out []settingsHook
	if arr, ok := existing.([]any); ok {
		for _, e := range arr {
			b, _ := json.Marshal(e)
			var sh settingsHook
			if json.Unmarshal(b, &sh) == nil && !containsCmd(sh, hookCmd) {
				out = append(out, sh)
			}
		}
	}
	return append(out, managed...)
}

func containsCmd(sh settingsHook, cmd string) bool {
	for _, h := range sh.Hooks {
		if h.Command == cmd {
			return true
		}
	}
	return false
}

// MapHook converts a received hook event into a status.change event, if any.
// Notification → needs_input (Claude is asking the user something); Stop → idle
// (the turn finished). PreToolUse/PostToolUse carry no status transition here
// (tool events come from the transcript) and return ok=false.
func MapHook(h HookEvent, prev event.Status) (event.Event, bool) {
	switch h.HookEventName {
	case "Notification":
		return event.StatusChange(h.SessionID, statusOr(prev, event.StatusActive), event.StatusNeedsInput), true
	case "Stop":
		return event.StatusChange(h.SessionID, statusOr(prev, event.StatusActive), event.StatusIdle), true
	case "PreToolUse":
		return event.StatusChange(h.SessionID, statusOr(prev, event.StatusIdle), event.StatusActive), true
	default:
		return event.Event{}, false
	}
}

func statusOr(s, fallback event.Status) event.Status {
	if event.ValidStatus(s) {
		return s
	}
	return fallback
}
