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

// TestLoadCredentials proves the shared credentials reader parses the
// "wsURL\ntoken\napiBase" file (CRLF tolerated), reports ok=false when the file
// is absent, and leaves missing lines empty.
func TestLoadCredentials(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	if _, ok := LoadCredentials(); ok {
		t.Fatal("missing credentials file must yield ok=false")
	}

	dir := filepath.Join(home, ".claude-plus")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	body := "wss://hq.example/ws\r\ntok-123\r\nhttps://hq.example\r\n"
	if err := os.WriteFile(filepath.Join(dir, "credentials"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	c, ok := LoadCredentials()
	if !ok {
		t.Fatal("LoadCredentials should succeed")
	}
	if c.WSURL != "wss://hq.example/ws" || c.Token != "tok-123" || c.APIBase != "https://hq.example" {
		t.Fatalf("parsed = %+v", c)
	}

	// A two-line legacy file leaves APIBase empty.
	if err := os.WriteFile(filepath.Join(dir, "credentials"), []byte("ws\ntok"), 0o600); err != nil {
		t.Fatal(err)
	}
	c, ok = LoadCredentials()
	if !ok || c.APIBase != "" || c.Token != "tok" {
		t.Fatalf("legacy file parsed = %+v ok=%v", c, ok)
	}
}

// TestRepoRootFor proves the shared repo-root resolver walks up to the nearest
// .git directory and falls back to the starting dir when there is none.
func TestRepoRootFor(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	nested := filepath.Join(root, "a", "b")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	if got := RepoRootFor(nested); got != root {
		t.Errorf("RepoRootFor(nested) = %q, want %q", got, root)
	}
	// No .git anywhere up the tree: falls back to the given dir. Guard against a
	// machine whose temp dir happens to live inside a git checkout.
	bare := t.TempDir()
	ancestorRepo := false
	for d := filepath.Dir(bare); ; d = filepath.Dir(d) {
		if fi, err := os.Stat(filepath.Join(d, ".git")); err == nil && fi.IsDir() {
			ancestorRepo = true
			break
		}
		if filepath.Dir(d) == d {
			break
		}
	}
	if !ancestorRepo {
		if got := RepoRootFor(bare); got != bare {
			t.Errorf("RepoRootFor(bare) = %q, want %q", got, bare)
		}
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
