package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

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

// TestRepoNameForPrefersRemote verifies RepoNameFor uses the git remote when
// available and falls back to the folder base name when there is no remote.
func TestRepoNameForPrefersRemote(t *testing.T) {
	orig := gitRemoteURL
	t.Cleanup(func() { gitRemoteURL = orig })

	gitRemoteURL = func(string) (string, bool) {
		return "git@github.com:acme/weekly-compass.git", true
	}
	if got := RepoNameFor("/some/path/workflow-harness"); got != "acme/weekly-compass" {
		t.Errorf("with remote: got %q, want acme/weekly-compass", got)
	}

	// No usable remote -> folder base name.
	gitRemoteURL = func(string) (string, bool) { return "", false }
	if got := RepoNameFor("/some/path/workflow-harness"); got != "workflow-harness" {
		t.Errorf("no remote: got %q, want workflow-harness", got)
	}

	// Remote present but unparseable -> folder base name.
	gitRemoteURL = func(string) (string, bool) { return "garbage", true }
	if got := RepoNameFor("/some/path/workflow-harness"); got != "workflow-harness" {
		t.Errorf("bad remote: got %q, want workflow-harness", got)
	}
}

// TestProjectIDForMatchesHQOwnerRepoSlug guards the per-project sync: the daemon's
// project id MUST equal HQ's slug of "owner/repo" (lowercase, non-alphanumeric
// runs -> single dash). If it diverges, GET /projects/{id} resolves to an empty
// project and the tight-mirror prune deletes every enabled catalog skill. Real
// case: repo MatthewDaw/fractions_tutorial -> HQ id matthewdaw-fractions-tutorial
// (NOT the folder-name slug fractions-tutorial).
func TestProjectIDForMatchesHQOwnerRepoSlug(t *testing.T) {
	orig := gitRemoteURL
	t.Cleanup(func() { gitRemoteURL = orig })

	gitRemoteURL = func(string) (string, bool) {
		return "git@github.com:MatthewDaw/fractions_tutorial.git", true
	}
	if got := ProjectIDFor("/x/fractions_tutorial"); got != "matthewdaw-fractions-tutorial" {
		t.Errorf("with remote: ProjectIDFor = %q, want matthewdaw-fractions-tutorial", got)
	}

	// No remote -> folder base-name slug (back-compat fallback).
	gitRemoteURL = func(string) (string, bool) { return "", false }
	if got := ProjectIDFor("/x/fractions_tutorial"); got != "fractions-tutorial" {
		t.Errorf("no remote: ProjectIDFor = %q, want fractions-tutorial", got)
	}
}

// TestEnsureConfigDirWritesManifest verifies EnsureConfigDir drops an
// hq-project.json manifest whose projectId matches ProjectIDFor for the repo's
// stubbed remote, so the launch path and tooling can read the id off disk.
func TestEnsureConfigDirWritesManifest(t *testing.T) {
	orig := gitRemoteURL
	t.Cleanup(func() { gitRemoteURL = orig })
	gitRemoteURL = func(string) (string, bool) {
		return "git@github.com:MatthewDaw/fractions_tutorial.git", true
	}

	// Point ~/.claude+ at a temp dir so the test never touches the real home.
	tmp := t.TempDir()
	t.Setenv("HOME", tmp)
	t.Setenv("USERPROFILE", tmp) // Windows home resolution

	root, err := EnsureConfigDir(filepath.Join(tmp, "fractions_tutorial"))
	if err != nil {
		t.Fatalf("EnsureConfigDir: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(root, "hq-project.json"))
	if err != nil {
		t.Fatalf("read manifest: %v", err)
	}
	var m struct {
		ProjectID string `json:"projectId"`
		Repo      string `json:"repo"`
	}
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatalf("unmarshal manifest: %v", err)
	}
	if m.ProjectID != "matthewdaw-fractions-tutorial" {
		t.Errorf("manifest projectId = %q, want matthewdaw-fractions-tutorial", m.ProjectID)
	}
	if m.Repo != "MatthewDaw/fractions_tutorial" {
		t.Errorf("manifest repo = %q, want MatthewDaw/fractions_tutorial", m.Repo)
	}
}
