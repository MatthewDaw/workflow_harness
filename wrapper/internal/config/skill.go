package config

// Skill whole-directory storage (U-Skill-Dirs). A skill is no longer just a
// SKILL.md body: it can ship a whole directory tree — SKILL.md PLUS sibling
// scripts/resources — carried in the catalog's `files` map (relative path within
// the skill dir -> file contents). The wrapper must materialize the WHOLE tree on
// a pull and hash the WHOLE tree on read, so a sibling-file edit drifts (not just
// a SKILL.md edit).
//
// Hash parity + back-compat hinge on ONE canonical serialization of a skill's
// files used on BOTH sides (skillFilesCanonical):
//   - A legacy body-only skill (exactly one file, "SKILL.md") canonicalizes to the
//     raw SKILL.md bytes — byte-identical to the pre-U-Skill-Dirs hash — so legacy
//     HQ records and legacy on-disk single-file skills still read back in-sync.
//   - A multi-file skill canonicalizes to a deterministic, path-sorted
//     concatenation of every file, so any sibling edit changes the hash.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// skillFilesEnvelopePrefix tags a Body() string that carries a whole skill-file
// MAP (multi-file skill) rather than a raw SKILL.md body. The prefix is not valid
// Markdown frontmatter and never begins a real SKILL.md, so ApplyPulled can
// unambiguously tell a multi-file envelope from a legacy single-body skill. A
// single-file (legacy) skill is still carried as the raw SKILL.md text, so the
// existing body-only path (and its hash) is completely unchanged.
const skillFilesEnvelopePrefix = "\x00skillfiles:"

// encodeSkillBody serializes a skill's files map into the Body() string the
// wrapper threads from Fetch to ApplyPulled. A single SKILL.md is returned as its
// raw body (legacy form, unchanged). A multi-file skill is returned as the
// envelope prefix followed by a deterministic JSON object (path -> contents), so
// ApplyPulled can rebuild the whole tree. The hash is computed separately by
// hashSkillFiles over the SAME map, so encode/decode never affects drift parity.
func encodeSkillBody(files map[string]string) string {
	if len(files) == 1 {
		if body, ok := files[skillMainFile]; ok {
			return body
		}
	}
	// Sort keys for a stable envelope (json.Marshal already sorts map keys, but be
	// explicit so the body is deterministic regardless of Go version).
	slashed := make(map[string]string, len(files))
	for p, c := range files {
		slashed[filepath.ToSlash(p)] = c
	}
	b, _ := json.Marshal(slashed)
	return skillFilesEnvelopePrefix + string(b)
}

// decodeSkillBody is the inverse of encodeSkillBody: a body carrying the envelope
// prefix decodes back to its files map; any other body is a legacy single-file
// skill and decodes to {"SKILL.md": body}. A malformed envelope falls back to
// treating the whole string as a SKILL.md body (never loses content).
func decodeSkillBody(body string) map[string]string {
	if rest, ok := strings.CutPrefix(body, skillFilesEnvelopePrefix); ok {
		var files map[string]string
		if err := json.Unmarshal([]byte(rest), &files); err == nil && files != nil {
			return files
		}
	}
	return map[string]string{skillMainFile: body}
}

// skillMainFile is the canonical entry file every skill must contain.
const skillMainFile = "SKILL.md"

// skillFilesCanonical produces the deterministic byte serialization of a skill's
// file set that BOTH the read side (ReadLocal) and the fetch side (remote.Fetch)
// hash, so a pulled skill reads back in-sync. Back-compat is preserved by a special
// case: a single SKILL.md collapses to exactly its (line-ending-normalized) body,
// matching the legacy raw-body hash. Multi-file skills serialize every file with a
// path header so a sibling edit shows as drift. Paths are slash-normalized and
// sorted so the result is independent of map iteration order and host separator.
func skillFilesCanonical(files map[string]string) []byte {
	// Legacy single-file fast path: byte-identical to the old raw-body hashing.
	if len(files) == 1 {
		if body, ok := files[skillMainFile]; ok {
			return []byte(normalizeText(body))
		}
	}
	paths := make([]string, 0, len(files))
	for p := range files {
		paths = append(paths, filepath.ToSlash(p))
	}
	sort.Strings(paths)
	// Rebuild a slash-keyed view so lookup matches the normalized path.
	bySlash := make(map[string]string, len(files))
	for p, c := range files {
		bySlash[filepath.ToSlash(p)] = c
	}
	var b strings.Builder
	for _, p := range paths {
		// A NUL-delimited path header cannot appear in a real relative path or in
		// text content we normalize, so it unambiguously frames each file.
		b.WriteString("\x00file:")
		b.WriteString(p)
		b.WriteString("\x00\n")
		b.WriteString(normalizeText(bySlash[p]))
		b.WriteString("\n")
	}
	return []byte(b.String())
}

// hashSkillFiles is the skill-tree hash used on both sides. It hashes the canonical
// serialization through hashContent so CRLF/LF and trailing-whitespace differences
// never register as drift (consistent with every other hash in this package).
func hashSkillFiles(files map[string]string) string {
	return hashContent(skillFilesCanonical(files))
}

// normalizeText applies the same CRLF->LF normalization hashContent uses, WITHOUT
// the outer trim (skillFilesCanonical frames files explicitly; trimming happens
// once in hashContent over the whole serialization). Kept tiny and local so the
// per-file body in the multi-file form matches the single-file fast path.
func normalizeText(s string) string {
	return strings.ReplaceAll(s, "\r\n", "\n")
}

// readSkillDir walks a skill directory under root (root/<name>/) and returns its
// files map (relative slash path -> contents). It reads the WHOLE tree so a
// sibling script/resource edit is visible to the hash (U-Skill-Dirs). An
// unreadable file makes the whole skill unreadable (returns ok=false) so the
// caller flags it as an Err item rather than hashing a partial tree. A directory
// with no SKILL.md still returns its files (the caller decides validity); an empty
// or absent directory returns an empty map.
func readSkillDir(dir string) (files map[string]string, ok bool) {
	files = map[string]string{}
	err := filepath.Walk(dir, func(p string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if info.IsDir() {
			return nil
		}
		rel, relErr := filepath.Rel(dir, p)
		if relErr != nil {
			return relErr
		}
		b, readErr := os.ReadFile(p)
		if readErr != nil {
			return readErr
		}
		files[filepath.ToSlash(rel)] = string(b)
		return nil
	})
	if err != nil {
		return files, false
	}
	return files, true
}

// writeSkillDir materializes a skill's files map into root/<name>/, creating parent
// directories as needed. It is additive (it never deletes files the map omits), so
// a pull that ships fewer files than exist locally leaves the extras in place —
// matching the additive, reversible contract of ApplyPulled. Each relative path is
// cleaned and constrained to stay within the skill dir (a path escaping via ".."
// is rejected) so a malformed catalog record cannot write outside the tree.
func writeSkillDir(skillRoot string, files map[string]string) error {
	for rel, content := range files {
		clean := filepath.Clean(filepath.FromSlash(rel))
		if clean == ".." || strings.HasPrefix(clean, ".."+string(os.PathSeparator)) || filepath.IsAbs(clean) {
			return &skillPathError{rel: rel}
		}
		dst := filepath.Join(skillRoot, clean)
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(dst, []byte(content), 0o644); err != nil {
			return err
		}
	}
	return nil
}

type skillPathError struct{ rel string }

func (e *skillPathError) Error() string {
	return "skill file path escapes skill dir: " + e.rel
}
