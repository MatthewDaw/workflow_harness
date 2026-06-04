package config

import (
	"os"
	"path/filepath"
	"testing"
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

func TestEnsureConfigDir_SeedsAuthOnceAndIsStable(t *testing.T) {
	home := setupHome(t)

	dir, err := EnsureConfigDir()
	if err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if dir != filepath.Join(home, ".claude+") {
		t.Fatalf("config dir = %s, want ~/.claude+", dir)
	}

	// Auth + settings + onboarding seeded from ~/.claude so the first launch is
	// already signed in.
	for _, name := range []string{".credentials.json", "settings.json", ".claude.json"} {
		if !pathExists(filepath.Join(dir, name)) {
			t.Errorf("%s was not seeded into ~/.claude+", name)
		}
	}

	// Stable across calls: a credential refreshed by claude+ is NOT clobbered by a
	// second EnsureConfigDir (seed only when absent), so auth persists.
	refreshed := filepath.Join(dir, ".credentials.json")
	if err := os.WriteFile(refreshed, []byte(`{"token":"refreshed"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := EnsureConfigDir(); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(refreshed)
	if string(b) != `{"token":"refreshed"}` {
		t.Errorf("EnsureConfigDir clobbered claude+'s own credential: %s", b)
	}
}

func TestEnsureConfigDir_NeverWritesToUserClaude(t *testing.T) {
	home := setupHome(t)
	if _, err := EnsureConfigDir(); err != nil {
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

	if _, ok := ConfigDir(); ok {
		t.Fatal("ConfigDir should be inactive before EnsureConfigDir")
	}
	if _, err := EnsureConfigDir(); err != nil {
		t.Fatal(err)
	}
	dir, ok := ConfigDir()
	if !ok || dir != filepath.Join(home, ".claude+") {
		t.Fatalf("ConfigDir active=%v dir=%s after init", ok, dir)
	}
}
