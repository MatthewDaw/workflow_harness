package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// setupHome points the home dir at a temp dir and seeds a ~/.claude with auth +
// settings files, returning the home path.
func setupHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	claude := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claude, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(claude, ".credentials.json"), []byte(`{"token":"abc"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(claude, "settings.json"), []byte(`{"a":1}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".claude.json"), []byte(`{"onboarded":true}`), 0o644); err != nil {
		t.Fatal(err)
	}
	return home
}

func TestEnsureConfigDir_SeedsAuthIntoPerProjectRootAndIsStable(t *testing.T) {
	home := setupHome(t)
	repo := filepath.Join(home, "repos", "demo")

	dir, err := EnsureConfigDir(repo)
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	// The returned dir is this repo's PER-PROJECT root under ~/.claude+/roots/,
	// not the shared base.
	want, _ := ProjectConfigDir(repo)
	if dir != want {
		t.Fatalf("config dir = %s, want per-project root %s", dir, want)
	}
	if filepath.Dir(filepath.Dir(dir)) != filepath.Join(home, ".claude+") {
		t.Fatalf("per-project root %s is not under ~/.claude+/roots/", dir)
	}

	// Auth + settings + onboarding flow from ~/.claude → base → this root, so the
	// first launch is already signed in.
	for _, name := range []string{".credentials.json", "settings.json", ".claude.json"} {
		if !pathExists(filepath.Join(dir, name)) {
			t.Errorf("%s was not synced into the per-project root", name)
		}
	}

	// Stable across calls: a credential the inner Claude refreshed in this root is
	// the NEWER copy, so the bidirectional base<->root sync keeps it (and pushes it
	// up to the base) rather than clobbering it with the older base copy.
	refreshed := filepath.Join(dir, ".credentials.json")
	if err := os.WriteFile(refreshed, []byte(`{"token":"refreshed"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	// Make the root copy unambiguously newer than the base copy regardless of FS
	// mtime granularity.
	future := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(refreshed, future, future); err != nil {
		t.Fatal(err)
	}
	if _, err := EnsureConfigDir(repo); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(refreshed)
	if string(b) != `{"token":"refreshed"}` {
		t.Errorf("EnsureConfigDir clobbered the root's refreshed credential: %s", b)
	}
	// The newer root credential is propagated up to the base for other projects.
	base, _ := os.ReadFile(filepath.Join(home, ".claude+", ".credentials.json"))
	if string(base) != `{"token":"refreshed"}` {
		t.Errorf("refreshed credential was not written back to the base: %s", base)
	}
}

func TestEnsureConfigDir_NeverWritesToUserClaude(t *testing.T) {
	home := setupHome(t)
	repo := filepath.Join(home, "repos", "demo")
	if _, err := EnsureConfigDir(repo); err != nil {
		t.Fatal(err)
	}
	// ~/.claude keeps exactly what we seeded — nothing new is written there.
	entries, err := os.ReadDir(filepath.Join(home, ".claude"))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		switch e.Name() {
		case ".credentials.json", "settings.json":
		default:
			t.Errorf("unexpected entry created in ~/.claude: %s", e.Name())
		}
	}
}

func TestConfigDir_ReportsActiveOnlyAfterInit(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := filepath.Join(home, "repos", "demo")

	if _, ok := ConfigDir(repo); ok {
		t.Fatal("ConfigDir should be inactive before EnsureConfigDir")
	}
	if _, err := EnsureConfigDir(repo); err != nil {
		t.Fatal(err)
	}
	dir, ok := ConfigDir(repo)
	want, _ := ProjectConfigDir(repo)
	if !ok || dir != want {
		t.Fatalf("ConfigDir active=%v dir=%s after init, want %s", ok, dir, want)
	}
}
