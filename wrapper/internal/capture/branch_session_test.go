package capture

import (
	"testing"
)

// TestIsBashGitPush exercises IsBashGitPush across the cases the acceptance
// checklist requires: successful push detection, failed-push detection, and
// detached-HEAD / non-push Bash invocations that must NOT produce a link.
func TestIsBashGitPush(t *testing.T) {
	cases := []struct {
		name      string
		toolName  string
		toolInput string
		want      bool
	}{
		// Positive cases — various git push forms.
		{
			name:      "simple git push",
			toolName:  "Bash",
			toolInput: `{"command":"git push"}`,
			want:      true,
		},
		{
			name:      "git push with remote",
			toolName:  "Bash",
			toolInput: `{"command":"git push origin"}`,
			want:      true,
		},
		{
			name:      "git push with remote and branch",
			toolName:  "Bash",
			toolInput: `{"command":"git push origin feat/my-feature"}`,
			want:      true,
		},
		{
			name:      "git push with set-upstream",
			toolName:  "Bash",
			toolInput: `{"command":"git push --set-upstream origin feat/foo"}`,
			want:      true,
		},
		{
			name:      "git push with -u flag",
			toolName:  "Bash",
			toolInput: `{"command":"git push -u origin feat/foo"}`,
			want:      true,
		},
		{
			name:      "git push in multi-command sequence",
			toolName:  "Bash",
			toolInput: `{"command":"git add -A && git commit -m 'wip' && git push origin main"}`,
			want:      true,
		},
		// Negative cases — NOT a git push.
		{
			name:      "wrong tool name",
			toolName:  "Read",
			toolInput: `{"command":"git push"}`,
			want:      false,
		},
		{
			name:      "not a Bash tool (lowercase)",
			toolName:  "bash",
			toolInput: `{"command":"git push"}`,
			want:      false,
		},
		{
			name:      "git commit only",
			toolName:  "Bash",
			toolInput: `{"command":"git commit -m 'foo'"}`,
			want:      false,
		},
		{
			name:      "git pull (not push)",
			toolName:  "Bash",
			toolInput: `{"command":"git pull origin"}`,
			want:      false,
		},
		{
			name:      "ls command",
			toolName:  "Bash",
			toolInput: `{"command":"ls -la"}`,
			want:      false,
		},
		{
			name:      "malformed JSON tool_input",
			toolName:  "Bash",
			toolInput: `not-json`,
			want:      false,
		},
		{
			name:      "empty tool_input",
			toolName:  "Bash",
			toolInput: ``,
			want:      false,
		},
		{
			name:      "tool_input without command field",
			toolName:  "Bash",
			toolInput: `{"script":"git push"}`,
			want:      false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := HookEvent{
				HookEventName: "PostToolUse",
				SessionID:     "sess-abc",
				ToolName:      tc.toolName,
				ToolInput:     tc.toolInput,
			}
			got := IsBashGitPush(h)
			if got != tc.want {
				t.Errorf("IsBashGitPush(%q / %q) = %v, want %v", tc.toolName, tc.toolInput, got, tc.want)
			}
		})
	}
}

// TestParsePushBranch verifies branch name extraction from git push command strings.
func TestParsePushBranch(t *testing.T) {
	cases := []struct {
		cmd  string
		want string
	}{
		{"git push", ""},
		{"git push origin", ""},
		{"git push origin feat/foo", "feat/foo"},
		{"git push origin HEAD:feat/foo", "feat/foo"},
		{"git push --set-upstream origin feat/my-feature", "feat/my-feature"},
		{"git push -u origin feat/my-feature", "feat/my-feature"},
		{"git push origin main", "main"},
		// No remote given — "feat/foo" is treated as the remote, so branch is unknown.
		{"git push feat/foo", ""},
	}

	for _, tc := range cases {
		got := ParsePushBranch(tc.cmd)
		if got != tc.want {
			t.Errorf("ParsePushBranch(%q) = %q, want %q", tc.cmd, got, tc.want)
		}
	}
}

// TestParsePushFailure verifies failure detection from tool output.
func TestParsePushFailure(t *testing.T) {
	cases := []struct {
		output string
		want   bool
	}{
		{"", false},
		{"Everything up-to-date", false},
		{"Branch 'feat/foo' set up to track remote branch.", false},
		{"error: failed to push some refs", true},
		{"fatal: 'origin' does not appear to be a git repository", true},
		{"[rejected]  main -> main (fetch first)", true},
		{"Failed to push refs to the remote repository", true},
		{"remote: Resolving deltas: 100% (2/2), done.", false},
	}

	for _, tc := range cases {
		got := ParsePushFailure(tc.output)
		if got != tc.want {
			t.Errorf("ParsePushFailure(%q) = %v, want %v", tc.output, got, tc.want)
		}
	}
}

// TestIsDetachedHead verifies detached-HEAD detection.
func TestIsDetachedHead(t *testing.T) {
	cases := []struct {
		ref  string
		want bool
	}{
		{"refs/heads/feat/foo", false},
		{"main", false},
		{"HEAD detached at abc1234", true},
		{"a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2", true}, // 40-char hex SHA
		{"short", false},
	}

	for _, tc := range cases {
		got := IsDetachedHead(tc.ref)
		if got != tc.want {
			t.Errorf("IsDetachedHead(%q) = %v, want %v", tc.ref, got, tc.want)
		}
	}
}

// TestScrubSecrets verifies the secret-scrub patterns used before storing
// distilledContext.
func TestScrubSecrets(t *testing.T) {
	cases := []struct {
		name    string
		input   string
		wantIn  string // substring that must NOT appear in the output
		okIn    string // substring that may still appear
	}{
		{
			name:   "AWS AKIA key",
			input:  "export AWS_KEY=AKIAIOSFODNN7EXAMPLE and more text",
			wantIn: "AKIAIOSFODNN7EXAMPLE",
		},
		{
			name:   "GitHub PAT",
			input:  "token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ12345678",
			wantIn: "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ12345678",
		},
		{
			name:   "PEM header",
			input:  "-----BEGIN RSA PRIVATE KEY----- ... rest",
			wantIn: "-----BEGIN RSA PRIVATE KEY-----",
		},
		{
			name:   "Bearer token",
			input:  "Authorization: Bearer eyXXXtokenXXX",
			wantIn: "Bearer eyXXXtokenXXX",
		},
		{
			name:   "safe text untouched",
			input:  "git push origin feat/foo",
			wantIn: "",
			okIn:   "git push origin feat/foo",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ScrubSecrets(tc.input)
			if tc.wantIn != "" && contains(got, tc.wantIn) {
				t.Errorf("ScrubSecrets(%q): output still contains secret %q\n  got: %q", tc.input, tc.wantIn, got)
			}
			if tc.okIn != "" && !contains(got, tc.okIn) {
				t.Errorf("ScrubSecrets(%q): safe text %q was unexpectedly removed\n  got: %q", tc.input, tc.okIn, got)
			}
		})
	}
}

// TestHookEventToolFieldsPresent verifies that HookEvent now carries ToolName
// and ToolInput (the U7 field additions).
func TestHookEventToolFieldsPresent(t *testing.T) {
	h := HookEvent{
		HookEventName: "PostToolUse",
		SessionID:     "s1",
		ToolName:      "Bash",
		ToolInput:     `{"command":"git push origin feat/bar"}`,
	}
	if h.ToolName != "Bash" {
		t.Errorf("ToolName: got %q, want %q", h.ToolName, "Bash")
	}
	if h.ToolInput == "" {
		t.Error("ToolInput is empty; field was not added to HookEvent")
	}
	if !IsBashGitPush(h) {
		t.Error("IsBashGitPush should be true for a Bash git-push PostToolUse event")
	}
}

// helper: string contains check (avoids importing strings in test file)
func contains(s, sub string) bool {
	return len(sub) > 0 && len(s) >= len(sub) &&
		func() bool {
			for i := 0; i+len(sub) <= len(s); i++ {
				if s[i:i+len(sub)] == sub {
					return true
				}
			}
			return false
		}()
}
