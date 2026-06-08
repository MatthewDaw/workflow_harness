package config

import (
	"os"
	"path/filepath"
	"strings"
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

// TestCopyFileAtomicPreservesMtime proves copyFile produces complete content,
// preserves the source mtime (so a converged auth pair stops re-copying), and
// leaves no orphan temp file behind.
func TestCopyFileAtomicPreservesMtime(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "src")
	dst := filepath.Join(dir, "dst")
	if err := os.WriteFile(src, []byte("hello-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-1 * time.Hour)
	if err := os.Chtimes(src, old, old); err != nil {
		t.Fatal(err)
	}
	if err := copyFile(src, dst); err != nil {
		t.Fatalf("copyFile: %v", err)
	}
	if b, _ := os.ReadFile(dst); string(b) != "hello-token" {
		t.Fatalf("content = %q", b)
	}
	si, _ := os.Stat(src)
	di, _ := os.Stat(dst)
	if !si.ModTime().Equal(di.ModTime()) {
		t.Fatalf("mtime not preserved: src=%v dst=%v", si.ModTime(), di.ModTime())
	}
	ents, _ := os.ReadDir(dir)
	for _, e := range ents {
		if strings.HasPrefix(e.Name(), ".tmp-") {
			t.Errorf("orphan temp file left behind: %s", e.Name())
		}
	}
}

// TestSyncAuthFileZeroLengthNeverWins proves a zero-length (corrupt/interrupted)
// auth file is never propagated over a good one, even when it is the NEWER copy —
// the guard against a truncated credential logging the user out everywhere.
func TestSyncAuthFileZeroLengthNeverWins(t *testing.T) {
	base := t.TempDir()
	root := t.TempDir()
	good := `{"token":"good"}`
	if err := os.WriteFile(filepath.Join(base, ".credentials.json"), []byte(good), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".credentials.json"), []byte(``), 0o600); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(time.Hour) // make the EMPTY file the newer one
	if err := os.Chtimes(filepath.Join(root, ".credentials.json"), future, future); err != nil {
		t.Fatal(err)
	}
	syncAuthFile(base, root, ".credentials.json")
	if b, _ := os.ReadFile(filepath.Join(base, ".credentials.json")); string(b) != good {
		t.Fatalf("good base credential was clobbered by the empty newer root file: %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(root, ".credentials.json")); string(b) != good {
		t.Fatalf("empty root credential was not healed from the base: %q", b)
	}
}

// TestSyncAuthFileConvergesNoChurn proves that after one sync the two copies are
// mtime-equal (copyFile preserves the source mtime), so a second sync is a true
// no-op rather than the per-spawn ping-pong the old code produced.
func TestSyncAuthFileConvergesNoChurn(t *testing.T) {
	base := t.TempDir()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(base, ".credentials.json"), []byte(`{"token":"x"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	syncAuthFile(base, root, ".credentials.json") // base -> root, mtime preserved
	ri, _ := os.Stat(filepath.Join(root, ".credentials.json"))
	syncAuthFile(base, root, ".credentials.json") // must not re-copy
	ri2, _ := os.Stat(filepath.Join(root, ".credentials.json"))
	if !ri2.ModTime().Equal(ri.ModTime()) {
		t.Fatalf("converged pair re-copied (churn): %v -> %v", ri.ModTime(), ri2.ModTime())
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
