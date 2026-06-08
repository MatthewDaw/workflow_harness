package config

import (
	"os"
	"path/filepath"
	"testing"
)

// skillRemoteItem builds the RemoteItem + encoded body for a skill exactly as
// remote.Fetch would, so a test can drive the same pull round-trip the daemon does.
func skillRemoteItem(s remoteSkill) (RemoteItem, string) {
	files := skillFilesFor(s)
	return RemoteItem{Kind: KindSkill, Name: s.Name, Scope: "org", Hash: hashSkillFiles(files)}, encodeSkillBody(files)
}

// TestSkillSingleFileHashMatchesLegacy proves a single-SKILL.md skill hashes
// byte-identically to the legacy raw-body form, so legacy records/on-disk skills
// stay in-sync after U-Skill-Dirs.
func TestSkillSingleFileHashMatchesLegacy(t *testing.T) {
	body := "# My Skill\nDo the thing.\n"
	legacy := hashContent([]byte(body))
	tree := hashSkillFiles(map[string]string{skillMainFile: body})
	if legacy != tree {
		t.Fatalf("single-file skill hash changed: legacy=%s tree=%s", legacy, tree)
	}
	// And the encoded body is the raw body (no envelope) for the single-file case.
	if got := encodeSkillBody(map[string]string{skillMainFile: body}); got != body {
		t.Fatalf("single-file body should be raw, got %q", got)
	}
}

// TestSkillWholeDirRoundTripInSync proves a multi-file skill from HQ -> encoded
// body -> ApplyPulled writes the WHOLE tree, and ReadLocal hashes it back in-sync.
func TestSkillWholeDirRoundTripInSync(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	s := remoteSkill{
		Name: "deploy",
		Files: map[string]string{
			"SKILL.md":         "# Deploy\nRun scripts/deploy.sh.\n",
			"scripts/deploy.sh": "#!/bin/sh\necho deploying\n",
			"reference/notes.md": "Some reference notes.\n",
		},
	}
	ri, body := skillRemoteItem(s)
	if err := ApplyPulled(testPlus(t), ri, body); err != nil {
		t.Fatalf("ApplyPulled: %v", err)
	}

	// The whole tree must be on disk, not just SKILL.md.
	plus := testPlus(t)
	for _, rel := range []string{"SKILL.md", "scripts/deploy.sh", "reference/notes.md"} {
		p := filepath.Join(plus, "skills", "deploy", filepath.FromSlash(rel))
		if _, err := os.Stat(p); err != nil {
			t.Errorf("expected materialized file %s: %v", rel, err)
		}
	}

	local, err := ReadLocal(plus)
	if err != nil {
		t.Fatalf("ReadLocal: %v", err)
	}
	report := Diff(local, []RemoteItem{ri})
	if !report.InSync() {
		t.Fatalf("whole-dir skill round-trip should be in sync, got %+v (rows=%+v)", report, report.Rows)
	}
}

// TestSkillSiblingEditDrifts proves editing a SIBLING file (not SKILL.md) makes the
// skill drift — the whole point of hashing the tree (U-Skill-Dirs).
func TestSkillSiblingEditDrifts(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	s := remoteSkill{
		Name: "deploy",
		Files: map[string]string{
			"SKILL.md":          "# Deploy\n",
			"scripts/deploy.sh": "echo v1\n",
		},
	}
	ri, body := skillRemoteItem(s)
	if err := ApplyPulled(testPlus(t), ri, body); err != nil {
		t.Fatalf("ApplyPulled: %v", err)
	}

	// Edit a sibling file locally; SKILL.md is untouched.
	sibling := filepath.Join(testPlus(t), "skills", "deploy", "scripts", "deploy.sh")
	if err := os.WriteFile(sibling, []byte("echo v2-edited\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	local, err := ReadLocal(testPlus(t))
	if err != nil {
		t.Fatalf("ReadLocal: %v", err)
	}
	report := Diff(local, []RemoteItem{ri})
	if report.Differs != 1 {
		t.Fatalf("a sibling edit should drift (differs=1), got %+v (rows=%+v)", report, report.Rows)
	}
}

// TestSkillDirMissingMainIsErr proves a skill directory without SKILL.md surfaces
// as an Err item (the verify gate then fails loudly) rather than hashing a partial
// tree.
func TestSkillDirMissingMainIsErr(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	dir := filepath.Join(testPlus(t), "skills", "broken")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "notes.md"), []byte("orphan"), 0o644); err != nil {
		t.Fatal(err)
	}

	local, err := ReadLocal(testPlus(t))
	if err != nil {
		t.Fatalf("ReadLocal: %v", err)
	}
	var sawErr bool
	for _, it := range local {
		if it.Kind == KindSkill && it.Name == "broken" && it.Err != "" {
			sawErr = true
		}
	}
	if !sawErr {
		t.Fatalf("skill dir missing SKILL.md should be flagged, got %+v", local)
	}
}

// TestWriteSkillDirRejectsEscape proves a malformed catalog path escaping the skill
// dir is rejected (never writes outside the tree).
func TestWriteSkillDirRejectsEscape(t *testing.T) {
	root := t.TempDir()
	err := writeSkillDir(filepath.Join(root, "skills", "x"), map[string]string{
		"../../evil.sh": "boom",
	})
	if err == nil {
		t.Fatal("expected an error for a path escaping the skill dir")
	}
}

// TestSkillFilesForBackfillsMain proves a `files` map that omits SKILL.md is
// backfilled from the Body so the entry file is never missing.
func TestSkillFilesForBackfillsMain(t *testing.T) {
	files := skillFilesFor(remoteSkill{
		Name: "x",
		Body: "# X\n",
		Files: map[string]string{
			"scripts/run.sh": "echo hi\n",
		},
	})
	if files[skillMainFile] != "# X\n" {
		t.Fatalf("SKILL.md should be backfilled from Body, got %q", files[skillMainFile])
	}
	if files["scripts/run.sh"] == "" {
		t.Fatal("sibling file should be preserved")
	}
}
