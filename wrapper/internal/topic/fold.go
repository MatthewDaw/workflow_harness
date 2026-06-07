package topic

import (
	"fmt"
	"regexp"
	"strings"
)

// Hard length caps on the two learning streams (D2). Applied AFTER the secret
// scrub so a truncation can never split a partially-redacted secret back into the
// clear.
const (
	maxImplLearning = 500
	maxDocQuestion  = 300
)

// Verdict mirrors judge.Verdict's fold-relevant fields. The topic package does
// not import internal/judge (the daemon owns the spawn seam and passes the parsed
// verdict in), so Fold takes this local shape — kept structurally identical.
type Verdict struct {
	SameTopic      bool
	TopicLabel     string
	Description    string
	IsCorrection   bool
	ContradictsDoc bool
	ImplLearning   string
	DocQuestion    string
}

// Learning is one folded, scrubbed, length-capped learning record the daemon
// emits as event.SessionLearning. Stream is "impl" or "doc"; DocRef is set only
// on the doc stream.
type Learning struct {
	Stream string
	Text   string
	DocRef string
}

// FoldResult is what Fold produces: the topic event payload (always emitted) plus
// zero, one, or two learning records, and the stable turn id used as the
// idempotency key on every emitted record for this turn.
type FoldResult struct {
	SegmentID   string
	TopicLabel  string
	Description string
	Learnings   []Learning
	TurnID      string
}

// Fold applies a validated verdict to the carried session state and returns the
// events to emit. It NEVER touches the stable slug/name (title-thrash guard). The
// topic switch is debounced: a confirmed same_topic:false holds ONE turn
// (PendingSwitch) before a fresh segment opens, so a transient blip does not
// fragment the timeline and the boundary turn's learnings attribute cleanly.
//
// Always, if ImplLearning != "" it appends an impl learning. If ContradictsDoc &&
// DocQuestion != "" it appends a doc learning carrying docRef. The rolling
// description advances to verdict.Description. The turn's touched files merge into
// the (possibly new) segment baseline. Learning text is scrubbed + length-capped
// here, before it can leave the machine (D2).
//
// docRef is the nearest-doc path the daemon resolved (or "" for none); it is only
// attached to a doc learning.
func (s *State) Fold(v Verdict, turnFiles []string, docRef string) FoldResult {
	// A judge call was spent: reset the cadence floor and count it.
	s.JudgeCalls++
	s.TurnsSinceJudge = 0
	s.TokensSinceJudge = 0
	s.TurnCounter++
	turnID := fmt.Sprintf("t%d", s.TurnCounter)

	switching := false
	if !v.SameTopic {
		if s.PendingSwitch || s.SegmentID == "" {
			// Confirmed (held one turn already, or the very first segment): switch.
			switching = true
			s.PendingSwitch = false
		} else {
			// First same_topic:false: hold one turn before fragmenting the timeline.
			s.PendingSwitch = true
		}
	} else {
		// Back on topic: clear any pending switch (the blip resolved).
		s.PendingSwitch = false
	}

	if switching {
		s.SegmentSeq++
		s.SegmentID = fmt.Sprintf("seg-%d", s.SegmentSeq)
		s.SegmentFiles = nil // new segment starts a fresh file baseline
	} else if s.SegmentID == "" {
		s.SegmentSeq++
		s.SegmentID = fmt.Sprintf("seg-%d", s.SegmentSeq)
	}

	if v.TopicLabel != "" {
		s.TopicLabel = v.TopicLabel
	}
	if v.Description != "" {
		s.Description = v.Description
	}
	s.SegmentFiles = mergeFiles(s.SegmentFiles, turnFiles)

	res := FoldResult{
		SegmentID:   s.SegmentID,
		TopicLabel:  s.TopicLabel,
		Description: s.Description,
		TurnID:      turnID,
	}
	// Impl learning is unconditional on a correction (the doc comparison never
	// diverts it away from the impl stream).
	if t := capLen(scrubSecrets(v.ImplLearning), maxImplLearning); t != "" {
		res.Learnings = append(res.Learnings, Learning{Stream: "impl", Text: t})
	}
	// Doc learning is additive and gated on an explicit contradiction.
	if v.ContradictsDoc {
		if t := capLen(scrubSecrets(v.DocQuestion), maxDocQuestion); t != "" {
			res.Learnings = append(res.Learnings, Learning{Stream: "doc", Text: t, DocRef: docRef})
		}
	}
	return res
}

// secretPatterns redact common credential shapes before a learning leaves the
// machine (D2). The list is intentionally conservative — over-redaction is
// preferable to a leaked secret, and the scrubbed token is unrecoverable.
var secretPatterns = []*regexp.Regexp{
	// AWS access key id.
	regexp.MustCompile(`AKIA[0-9A-Z]{16}`),
	// Generic long base64-ish secret/token assignments (key=..., "token": "...").
	regexp.MustCompile(`(?i)(secret|token|password|passwd|api[_-]?key|access[_-]?key)\s*[:=]\s*["']?[A-Za-z0-9_\-\.\/+]{12,}["']?`),
	// Bearer tokens.
	regexp.MustCompile(`(?i)bearer\s+[A-Za-z0-9_\-\.]{12,}`),
	// JWT-ish three-segment tokens.
	regexp.MustCompile(`eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+`),
	// Private key PEM headers.
	regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`),
	// GitHub-style PATs.
	regexp.MustCompile(`gh[posru]_[A-Za-z0-9]{20,}`),
}

const redacted = "[REDACTED]"

// scrubSecrets replaces any matched secret pattern with a fixed marker.
func scrubSecrets(s string) string {
	out := s
	for _, re := range secretPatterns {
		out = re.ReplaceAllString(out, redacted)
	}
	return out
}

// capLen trims whitespace and hard-caps a learning to n runes (rune-safe so a cap
// never splits a multibyte rune).
func capLen(s string, n int) string {
	s = strings.TrimSpace(s)
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return strings.TrimSpace(string(r[:n]))
}
