package daemon

import "testing"

// TestOwnerRepoFromRemote covers reducing assorted git remote URL shapes to a
// readable "owner/repo" (or "" when no meaningful pair can be extracted).
func TestOwnerRepoFromRemote(t *testing.T) {
	cases := []struct {
		url  string
		want string
	}{
		{"https://github.com/acme/weekly-compass.git", "acme/weekly-compass"},
		{"https://github.com/acme/weekly-compass", "acme/weekly-compass"},
		{"git@github.com:acme/weekly-compass.git", "acme/weekly-compass"},
		{"git@github.com:acme/weekly-compass", "acme/weekly-compass"},
		{"ssh://git@github.com/acme/weekly-compass.git", "acme/weekly-compass"},
		{"https://gitlab.com/group/sub/proj.git", "sub/proj"},
		{"git@github.com:acme/weekly-compass.git/", "acme/weekly-compass"},
		{"", ""},
		{"not-a-url", ""},
	}
	for _, c := range cases {
		if got := ownerRepoFromRemote(c.url); got != c.want {
			t.Errorf("ownerRepoFromRemote(%q) = %q, want %q", c.url, got, c.want)
		}
	}
}

// TestRepoNameForPrefersRemote verifies repoNameFor uses the git remote when
// available and falls back to the folder base name when there is no remote.
func TestRepoNameForPrefersRemote(t *testing.T) {
	orig := gitRemoteURL
	t.Cleanup(func() { gitRemoteURL = orig })

	gitRemoteURL = func(string) (string, bool) {
		return "git@github.com:acme/weekly-compass.git", true
	}
	if got := repoNameFor("/some/path/workflow-harness"); got != "acme/weekly-compass" {
		t.Errorf("with remote: got %q, want acme/weekly-compass", got)
	}

	// No usable remote -> folder base name.
	gitRemoteURL = func(string) (string, bool) { return "", false }
	if got := repoNameFor("/some/path/workflow-harness"); got != "workflow-harness" {
		t.Errorf("no remote: got %q, want workflow-harness", got)
	}

	// Remote present but unparseable -> folder base name.
	gitRemoteURL = func(string) (string, bool) { return "garbage", true }
	if got := repoNameFor("/some/path/workflow-harness"); got != "workflow-harness" {
		t.Errorf("bad remote: got %q, want workflow-harness", got)
	}
}
