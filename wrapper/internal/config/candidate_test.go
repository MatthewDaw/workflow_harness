package config

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// --- Characterization of hashSkillFiles (write BEFORE changing the materialize
// path, per U12's execution note — the drift/re-pull loop is timing-sensitive
// legacy and the hash basis must be pinned). These lock in the EXISTING contract
// so the candidate-block exclusion can be added without silently shifting drift. ---

// TestCharacterizeHashSingleFileEqualsLegacy pins the single-file fast path: a lone
// SKILL.md hashes byte-identically to the raw normalized body (legacy form). If this
// changes, every legacy skill on every machine would drift.
func TestCharacterizeHashSingleFileEqualsLegacy(t *testing.T) {
	body := "# Skill\nDo the thing.\n"
	if got, want := hashSkillFiles(map[string]string{skillMainFile: body}), hashContent([]byte(body)); got != want {
		t.Fatalf("single-file hash drifted from legacy: got %s want %s", got, want)
	}
}

// TestCharacterizeHashCRLFInvariant pins that CRLF vs LF never registers as drift
// (the cross-platform invariant the whole hash family relies on).
func TestCharacterizeHashCRLFInvariant(t *testing.T) {
	lf := map[string]string{skillMainFile: "# A\nline\n", "scripts/x.sh": "echo hi\n"}
	crlf := map[string]string{skillMainFile: "# A\r\nline\r\n", "scripts/x.sh": "echo hi\r\n"}
	if hashSkillFiles(lf) != hashSkillFiles(crlf) {
		t.Fatal("CRLF/LF must hash identically (drift invariant)")
	}
}

// TestCharacterizeHashSiblingEditChanges pins that a sibling-file edit DOES change
// the hash (the U-Skill-Dirs guarantee a sibling edit drifts).
func TestCharacterizeHashSiblingEditChanges(t *testing.T) {
	base := map[string]string{skillMainFile: "# A\n", "scripts/x.sh": "echo v1\n"}
	edited := map[string]string{skillMainFile: "# A\n", "scripts/x.sh": "echo v2\n"}
	if hashSkillFiles(base) == hashSkillFiles(edited) {
		t.Fatal("a sibling edit must change the hash")
	}
}

// TestCharacterizeHashOrderInvariant pins that map iteration order does not affect
// the hash (paths are sorted before serialization).
func TestCharacterizeHashOrderInvariant(t *testing.T) {
	a := map[string]string{skillMainFile: "# A\n", "b.md": "B\n", "a.md": "A\n"}
	// Same content, different literal construction order — must hash the same.
	b := map[string]string{"a.md": "A\n", skillMainFile: "# A\n", "b.md": "B\n"}
	if hashSkillFiles(a) != hashSkillFiles(b) {
		t.Fatal("hash must be independent of map order")
	}
}

// --- U12 behavior: the injected candidate-learnings block is excluded from the
// drift hash, refreshes cleanly, and an empty list injects nothing. ---

// TestCandidateBlockExcludedFromHash is the central guarantee: injecting a block
// into SKILL.md must NOT change the drift hash, so a corroboration change never
// forces a re-pull. Single-file AND multi-file skills must both hold.
func TestCandidateBlockExcludedFromHash(t *testing.T) {
	for _, tc := range []struct {
		name  string
		files map[string]string
	}{
		{"single-file", map[string]string{skillMainFile: "# Skill\nBody.\n"}},
		{"multi-file", map[string]string{skillMainFile: "# Skill\nBody.\n", "scripts/run.sh": "echo hi\n"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			before := hashSkillFiles(tc.files)

			block := renderCandidateBlock([]candidateLearning{{Text: "Always lint before commit."}})
			injected := map[string]string{}
			for p, c := range tc.files {
				injected[p] = c
			}
			injected[skillMainFile] = applyCandidateBlock(injected[skillMainFile], block)

			if !strings.Contains(injected[skillMainFile], candidateBegin) {
				t.Fatal("block was not injected into SKILL.md")
			}
			if got := hashSkillFiles(injected); got != before {
				t.Fatalf("injected block changed the drift hash: before=%s after=%s", before, got)
			}
		})
	}
}

// TestCandidateBlockRefreshDoesNotChangeHash proves a corroboration change (a
// DIFFERENT block next session) still hashes to the same canonical value — so the
// refresh never causes a re-pull storm.
func TestCandidateBlockRefreshDoesNotChangeHash(t *testing.T) {
	body := "# Skill\nBody.\n"
	canonicalHash := hashSkillFiles(map[string]string{skillMainFile: body})

	first := applyCandidateBlock(body, renderCandidateBlock([]candidateLearning{{Text: "Lesson one."}}))
	second := applyCandidateBlock(first, renderCandidateBlock([]candidateLearning{
		{Text: "Lesson one."},
		{Text: "A newly corroborated lesson two."},
	}))

	if first == second {
		t.Fatal("a corroboration change should rewrite the block")
	}
	if h := hashSkillFiles(map[string]string{skillMainFile: second}); h != canonicalHash {
		t.Fatalf("refreshed block changed the hash: %s != %s", h, canonicalHash)
	}
	// And the canonical body (block stripped) is unchanged across refreshes.
	if stripCandidateLearnings(second) != stripCandidateLearnings(first) {
		t.Fatal("canonical body must be stable across block refreshes")
	}
}

// TestEmptyCandidateListInjectsNothing proves an empty corroborated set injects no
// block, and removes any prior block (the "empty list injects nothing" contract).
func TestEmptyCandidateListInjectsNothing(t *testing.T) {
	body := "# Skill\nBody.\n"
	if got := applyCandidateBlock(body, renderCandidateBlock(nil)); got != body {
		t.Fatalf("empty list must inject nothing, got %q", got)
	}
	// A body that already carried a block has it removed when the set goes empty
	// (e.g. all ideas folded), returning to the canonical body.
	withBlock := applyCandidateBlock(body, renderCandidateBlock([]candidateLearning{{Text: "x"}}))
	if got := applyCandidateBlock(withBlock, renderCandidateBlock(nil)); got != body {
		t.Fatalf("emptying the set must remove the stale block, got %q", got)
	}
}

// TestCandidateBlockFencedAndSeparated proves the block is fenced (begin/end
// markers) and visually separated from the authored body by a blank line — so it
// reads as a distinct section, not part of the skill.
func TestCandidateBlockFencedAndSeparated(t *testing.T) {
	body := "# Skill\nThe authored body.\n"
	out := applyCandidateBlock(body, renderCandidateBlock([]candidateLearning{{Text: "Surfaced lesson."}}))

	if !strings.Contains(out, candidateBegin) || !strings.Contains(out, candidateEnd) {
		t.Fatalf("block must be fenced by begin/end markers:\n%s", out)
	}
	if !strings.Contains(out, candidateHeading) {
		t.Fatalf("block must carry a visible heading:\n%s", out)
	}
	// The authored body precedes the fence, separated by a blank line.
	idx := strings.Index(out, candidateBegin)
	if idx < 0 || !strings.HasPrefix(out[:idx], "# Skill\nThe authored body.\n\n") {
		t.Fatalf("authored body must precede the fence with a blank-line separator:\n%q", out[:idx])
	}
	// The end marker is last (the block is appended, not interleaved).
	if !strings.Contains(out, "Surfaced lesson.") {
		t.Fatalf("learning text must appear in the block:\n%s", out)
	}
}

// TestStripIsInverseOfInject proves stripping always recovers the canonical body,
// even when injection is applied repeatedly (idempotent round-trip).
func TestStripIsInverseOfInject(t *testing.T) {
	body := "# Skill\nAuthored.\n"
	once := applyCandidateBlock(body, renderCandidateBlock([]candidateLearning{{Text: "a"}}))
	twice := applyCandidateBlock(once, renderCandidateBlock([]candidateLearning{{Text: "b"}, {Text: "c"}}))
	if got := stripCandidateLearnings(twice); got != body {
		t.Fatalf("strip must recover the canonical body, got %q", got)
	}
}

// TestInjectCandidateLearningsEndToEnd drives the real HTTP source: a materialized
// skill gets its corroborated block written to disk, the on-disk skill still reads
// back IN-SYNC (no drift), and a fold (empty set) next session removes the block —
// all without the skill ever showing as needs_pull.
func TestInjectCandidateLearningsEndToEnd(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	// HQ serves an empty catalog (we materialize the skill directly) plus the U11
	// candidate-learnings endpoint whose payload we flip between calls.
	var learnings []map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "application/json")
		switch {
		case strings.HasSuffix(r.URL.Path, "/candidate-learnings"):
			_ = json.NewEncoder(w).Encode(map[string]any{"learnings": learnings})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")

	// Materialize a skill on disk exactly as a pull would, and capture HQ's hash.
	s := remoteSkill{Name: "deploy", Body: "# Deploy\nRun the steps.\n"}
	ri, body := skillRemoteItem(s)
	plus := testPlus(t)
	if err := ApplyPulled(plus, ri, body); err != nil {
		t.Fatalf("ApplyPulled: %v", err)
	}

	// First session: two corroborated learnings surface.
	learnings = []map[string]string{{"text": "Pin the image tag."}, {"text": "Drain before deploy."}}
	if errs := src.InjectCandidateLearnings(plus, []string{"deploy"}); len(errs) != 0 {
		t.Fatalf("inject errs: %v", errs)
	}

	skillPath := filepath.Join(plus, "skills", "deploy", skillMainFile)
	onDisk, err := os.ReadFile(skillPath)
	if err != nil {
		t.Fatalf("read skill: %v", err)
	}
	if !strings.Contains(string(onDisk), "Pin the image tag.") || !strings.Contains(string(onDisk), candidateBegin) {
		t.Fatalf("learnings block not on disk:\n%s", onDisk)
	}

	// The injected block must NOT make the skill drift: ReadLocal hashes the on-disk
	// file (block included) but the canonical strip keeps it equal to HQ's hash.
	assertInSync := func(stage string) {
		t.Helper()
		local, err := ReadLocal(plus)
		if err != nil {
			t.Fatalf("ReadLocal: %v", err)
		}
		report := Diff(local, []RemoteItem{ri})
		if !report.InSync() {
			t.Fatalf("%s: injected block caused drift (must not re-pull): %+v", stage, report.Rows)
		}
	}
	assertInSync("after first inject")

	// Next session: corroboration changes (a third learning). Re-inject and confirm
	// the block refreshed but the skill is STILL in sync (no re-pull storm).
	learnings = []map[string]string{{"text": "Pin the image tag."}, {"text": "Drain before deploy."}, {"text": "Verify health post-rollout."}}
	if errs := src.InjectCandidateLearnings(plus, []string{"deploy"}); len(errs) != 0 {
		t.Fatalf("re-inject errs: %v", errs)
	}
	refreshed, _ := os.ReadFile(skillPath)
	if !strings.Contains(string(refreshed), "Verify health post-rollout.") {
		t.Fatalf("block did not refresh with the new corroboration:\n%s", refreshed)
	}
	assertInSync("after refresh")

	// All ideas fold → empty set. Re-inject removes the block; still in sync.
	learnings = nil
	if errs := src.InjectCandidateLearnings(plus, []string{"deploy"}); len(errs) != 0 {
		t.Fatalf("empty inject errs: %v", errs)
	}
	final, _ := os.ReadFile(skillPath)
	if strings.Contains(string(final), candidateBegin) {
		t.Fatalf("empty set must remove the block:\n%s", final)
	}
	assertInSync("after empty")
}

// TestInjectCandidateLearningsMissingSkillIsNoop proves injecting for a skill with
// no on-disk SKILL.md (never materialized) is a silent no-op, not an error — so a
// stray declared name can't fail surfacing.
func TestInjectCandidateLearningsMissingSkillIsNoop(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("content-type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"learnings": []map[string]string{{"text": "x"}}})
	}))
	defer srv.Close()

	src := NewHTTPRemoteSource(srv.URL, "tok", "proj-1")
	if errs := src.InjectCandidateLearnings(testPlus(t), []string{"never-materialized"}); len(errs) != 0 {
		t.Fatalf("missing skill must be a no-op, got: %v", errs)
	}
}
