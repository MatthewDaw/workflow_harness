package config

import (
	"os"
	"path/filepath"
	"testing"
)

// setupHome points the home dir at a temp dir and seeds a ~/.claude with a
// credential + settings file, returning the home path.
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
	return home
}

func TestBuildSessionConfigDir_IsolatesFromUserClaude(t *testing.T) {
	home := setupHome(t)

	// Seed a product skill in the isolated source so the link has a target.
	plusSkills := filepath.Join(home, ".claude+", "skills", "startforge")
	if err := os.MkdirAll(plusSkills, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(plusSkills, "SKILL.md"), []byte("# startforge"), 0o644); err != nil {
		t.Fatal(err)
	}

	dir, cleanup, err := BuildSessionConfigDir("sess-1")
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	defer cleanup()

	// The config root is under ~/.claude+/run, never ~/.claude.
	wantPrefix := filepath.Join(home, ".claude+", "run")
	if rel, _ := filepath.Rel(wantPrefix, dir); rel == "" || rel[:2] == ".." {
		t.Fatalf("config dir %s is not under %s", dir, wantPrefix)
	}

	// Settings were copied (a snapshot), so editing the snapshot can't touch the
	// user's real file.
	if _, err := os.Stat(filepath.Join(dir, "settings.json")); err != nil {
		t.Errorf("settings.json not copied into session root: %v", err)
	}

	// The product skill is reachable through the session root's skills link.
	if _, err := os.Stat(filepath.Join(dir, "skills", "startforge", "SKILL.md")); err != nil {
		t.Errorf("product skill not linked into session root: %v", err)
	}

	// Nothing was written into the user's personal ~/.claude beyond what we seeded.
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

func TestBuildSessionConfigDir_CleanupRemovesSessionRootOnly(t *testing.T) {
	home := setupHome(t)

	dir, cleanup, err := BuildSessionConfigDir("sess-2")
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	cleanup()

	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Errorf("session root should be removed after cleanup, stat err = %v", err)
	}
	// The stable isolated skills source survives a session teardown.
	if _, err := os.Stat(filepath.Join(home, ".claude+", "skills")); err != nil {
		t.Errorf("isolated skills source should survive cleanup: %v", err)
	}
}

func TestBuildSessionConfigDir_LoggedOutHasNoCredentialLink(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	// No ~/.claude at all (logged out / fresh machine).

	dir, cleanup, err := BuildSessionConfigDir("sess-3")
	if err != nil {
		t.Fatalf("build should succeed when logged out: %v", err)
	}
	defer cleanup()

	if _, err := os.Lstat(filepath.Join(dir, ".credentials.json")); !os.IsNotExist(err) {
		t.Errorf("expected no credential link when ~/.claude absent, got err = %v", err)
	}
}

func TestReadLocal_UnionsClaudePlusWithoutPolluting(t *testing.T) {
	home := setupHome(t)

	// A user-owned skill in ~/.claude and a product skill in ~/.claude+.
	userSkill := filepath.Join(home, ".claude", "skills", "my-skill")
	if err := os.MkdirAll(userSkill, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(userSkill, "SKILL.md"), []byte("# mine"), 0o644); err != nil {
		t.Fatal(err)
	}
	plusSkill := filepath.Join(home, ".claude+", "skills", "startforge")
	if err := os.MkdirAll(plusSkill, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(plusSkill, "SKILL.md"), []byte("# forge"), 0o644); err != nil {
		t.Fatal(err)
	}

	items, err := ReadLocal()
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]bool{}
	for _, it := range items {
		if it.Kind == KindSkill {
			names[it.Name] = true
		}
	}
	if !names["my-skill"] || !names["startforge"] {
		t.Errorf("ReadLocal should union both roots; got %v", names)
	}

	// ApplyPulled writes into ~/.claude+, never ~/.claude.
	if err := ApplyPulled(RemoteItem{Kind: KindSkill, Name: "pulled"}, "# pulled"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(home, ".claude+", "skills", "pulled", "SKILL.md")); err != nil {
		t.Errorf("pulled skill should land in ~/.claude+: %v", err)
	}
	if _, err := os.Stat(filepath.Join(home, ".claude", "skills", "pulled")); !os.IsNotExist(err) {
		t.Errorf("pulled skill must NOT land in ~/.claude")
	}
}
