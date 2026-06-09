package config

// Candidate-learnings injection (U12). At session-start materialize the daemon
// fetches a skill's CORROBORATED candidate learnings from HQ
// (GET /skills/{name}/candidate-learnings) and writes them into the on-disk
// SKILL.md as a single clearly-fenced, visually-separated block. The agent then
// sees the surfaced lessons in-session.
//
// The block is deliberately EXCLUDED from the skill drift hash
// (skillFilesCanonical strips it before hashing on both sides). HQ never stores
// the block, so if it counted toward the hash every corroboration change would
// make the local SKILL.md differ from HQ and force a re-pull on every machine —
// the exact churn this design avoids. Stripping it keeps the canonical (block-
// free) body as the sole hash basis, so refreshing the block next session is
// invisible to drift.
//
// The injection is idempotent and refresh-safe: writing the block replaces any
// prior block in place (markers frame it), so a corroboration change next session
// rewrites the block without disturbing the canonical body or accreting stale
// copies.

import (
	"os"
	"path/filepath"
	"strings"
)

// candidateBegin / candidateEnd fence the injected block. They are HTML comments
// so they render invisibly in Markdown viewers yet are trivially machine-locatable
// for strip/replace. The marker text is intentionally distinctive so it never
// collides with real SKILL.md content.
const (
	candidateBegin = "<!-- BEGIN claude+ candidate-learnings (auto-injected; excluded from drift) -->"
	candidateEnd   = "<!-- END claude+ candidate-learnings -->"
)

// candidateHeading is the human-visible header inside the fenced block, so the
// section is clearly separated from the skill's authored body.
const candidateHeading = "## Candidate learnings (corroborated, surfaced read-only)"

// stripCandidateLearnings removes any injected candidate-learnings block (and the
// blank-line separation that precedes it) from a SKILL.md body, returning the
// canonical authored body. It is the inverse of injection and is what
// skillFilesCanonical uses to keep the block out of the drift hash. A body with no
// block is returned unchanged (after the same trailing normalization injection
// uses, so a re-strip is stable).
func stripCandidateLearnings(body string) string {
	norm := strings.ReplaceAll(body, "\r\n", "\n")
	start := strings.Index(norm, candidateBegin)
	if start < 0 {
		return body
	}
	endIdx := strings.Index(norm, candidateEnd)
	if endIdx < 0 {
		// A begin marker with no end is malformed; drop from the begin marker on so a
		// half-written block never pollutes the canonical body or the hash.
		return strings.TrimRight(norm[:start], "\n") + "\n"
	}
	tail := norm[endIdx+len(candidateEnd):]
	head := strings.TrimRight(norm[:start], "\n")
	rest := strings.TrimLeft(tail, "\n")
	if rest == "" {
		if head == "" {
			return ""
		}
		return head + "\n"
	}
	return head + "\n" + rest
}

// renderCandidateBlock builds the fenced, visually-separated block for a set of
// corroborated learnings. Returns "" for an empty set so callers inject nothing.
// The block is framed by the begin/end markers (so it round-trips through
// stripCandidateLearnings) and carries a heading plus one bullet per learning.
func renderCandidateBlock(learnings []candidateLearning) string {
	if len(learnings) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString(candidateBegin)
	b.WriteString("\n")
	b.WriteString(candidateHeading)
	b.WriteString("\n")
	b.WriteString("These are corroborated, session-derived suggestions surfaced read-only. They are NOT part of the skill body and are not yet folded in.\n\n")
	for _, l := range learnings {
		text := strings.TrimSpace(l.Text)
		if text == "" {
			continue
		}
		// Keep each learning on a single bullet; collapse internal newlines so the
		// block stays a clean list regardless of how the synthesized text was written.
		text = strings.ReplaceAll(strings.ReplaceAll(text, "\r\n", " "), "\n", " ")
		b.WriteString("- ")
		b.WriteString(text)
		b.WriteString("\n")
	}
	b.WriteString(candidateEnd)
	return b.String()
}

// applyCandidateBlock returns the SKILL.md body with the candidate-learnings block
// set to `block`: it first strips any existing block, then (when `block` is
// non-empty) appends it separated by a blank line. An empty `block` therefore
// removes a stale block — so a skill whose ideas all folded (or never corroborated)
// has nothing injected, exactly per the "empty list injects nothing" contract.
func applyCandidateBlock(body, block string) string {
	canonical := stripCandidateLearnings(body)
	if block == "" {
		return canonical
	}
	canonical = strings.TrimRight(strings.ReplaceAll(canonical, "\r\n", "\n"), "\n")
	if canonical == "" {
		return block + "\n"
	}
	return canonical + "\n\n" + block + "\n"
}

// injectCandidateBlock rewrites the candidate-learnings block in the on-disk
// SKILL.md for skill `name` under `plus` (skills/<name>/SKILL.md). It reads the
// current body, applies the rendered block (stripping any prior block first), and
// writes back only when the result differs (so an idempotent re-run touches no
// file and never bumps mtime needlessly). A skill directory with no SKILL.md is
// skipped without error — it was not materialized, so there is nothing to inject
// into. An empty learnings set removes any stale block.
func injectCandidateBlock(plus, name string, learnings []candidateLearning) error {
	path := filepath.Join(plus, "skills", name, skillMainFile)
	cur, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	block := renderCandidateBlock(learnings)
	next := applyCandidateBlock(string(cur), block)
	if next == string(cur) {
		return nil
	}
	return os.WriteFile(path, []byte(next), 0o644)
}

// candidateLearning is the wrapper-side shape of one corroborated learning served
// by GET /skills/{name}/candidate-learnings. Only the synthesized `text` is needed
// to render the block; the endpoint already filters to corroborated-open ideas and
// ranks/caps them, so the wrapper renders them verbatim in order.
type candidateLearning struct {
	Text string `json:"text"`
}
