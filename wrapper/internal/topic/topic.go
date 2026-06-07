// Package topic is the daemon brain for topic-focus logging (U6): the cheap gate
// that decides whether a finished turn is worth a judge call, the per-session
// topic/cadence/debounce state it folds a verdict into, the scrub + length caps
// applied before a learning leaves the machine, and the checkpoint store that
// survives a daemon restart.
//
// It is deliberately pure and side-effect free: the daemon's captureLoop owns the
// tailer, the transcript path, the emit closure and the judge spawn; this package
// only computes (gate decision, judge input shape, folded state + the events to
// emit). That keeps the brain unit-testable without a real daemon, a real tailer,
// or a real headless model.
package topic

import (
	"strings"
)

// correctionCues are the lowercase substrings whose presence in a turn's opening
// prompt smells like a manual correction of the assistant. The set is cheap and
// lexical by design (R2: the correction cue must fire independently of file
// churn, so same-file and no-edit corrections are not missed). Tuned to favor
// recall — a false-positive only costs one cheap Haiku call, which the judge then
// resolves with is_correction.
var correctionCues = []string{
	"no,", "no ", "actually", "that is wrong", "that's wrong", "you forgot",
	"instead", "revert", "not quite", "undo", "wrong", "incorrect", "don't",
	"do not", "should not", "shouldn't", "stop", "that's not", "that is not",
	"fix that", "not what", "remove that", "you missed", "rather",
}

// SmellsLikeCorrection reports whether an opening prompt contains a correction
// cue. Evaluated on UserPromptSubmit and stashed on the session gate state (D1).
func SmellsLikeCorrection(prompt string) bool {
	p := strings.ToLower(strings.TrimSpace(prompt))
	if p == "" {
		return false
	}
	for _, cue := range correctionCues {
		if strings.Contains(p, cue) {
			return true
		}
	}
	return false
}

// Gate thresholds (D3). jaccardFloor is the file-set similarity below which a
// turn is assumed to have shifted topic; cadenceTurns / cadenceTokens are the
// "spend a judge call at least this often" floor; maxJudgeCalls caps per-session
// judge spend.
const (
	jaccardFloor  = 0.3
	cadenceTurns  = 3
	cadenceTokens = 1500
	maxJudgeCalls = 50
)

// State is the per-session topic/cadence/debounce checkpoint. It is JSON-
// serializable (every field exported) so the daemon can persist it under the
// claude+ config dir keyed by tab session id and reload it on captureLoop start
// (C6). The zero value is a valid fresh session.
type State struct {
	// TopicLabel + Description are the carried topic state fed to the judge and
	// rolled forward on each fold. SegmentID is the current segment's id.
	TopicLabel  string `json:"topicLabel"`
	Description string `json:"description"`
	SegmentID   string `json:"segmentId"`
	// SegmentFiles is the accumulated write-class file set for the CURRENT segment,
	// the baseline the next turn's Jaccard shift is measured against.
	SegmentFiles []string `json:"segmentFiles"`
	// CorrectionPending is the stashed UserPromptSubmit correction cue, consumed by
	// the next Stop's gate (D1).
	CorrectionPending bool `json:"correctionPending"`
	// TurnsSinceJudge / TokensSinceJudge drive the cadence floor; reset on a judge
	// call. JudgeCalls is the per-session spend (capped at maxJudgeCalls).
	TurnsSinceJudge  int   `json:"turnsSinceJudge"`
	TokensSinceJudge int64 `json:"tokensSinceJudge"`
	JudgeCalls       int   `json:"judgeCalls"`
	// TurnCounter is the monotonic per-session turn id source. Each Stop increments
	// it; the value is the stable turnId on emitted learnings so a re-emit dedupes.
	TurnCounter int `json:"turnCounter"`
	// PendingSwitch debounces a topic switch: a confirmed same_topic:false holds
	// ONE turn before opening a new segment (title-thrash / description-bleed
	// guard). The next fold that still wants to switch actually switches.
	PendingSwitch bool `json:"pendingSwitch"`
	// SegmentSeq numbers segments so a fresh segmentId is stable + ordered.
	SegmentSeq int `json:"segmentSeq"`
	// LastOffset is the transcript byte offset already harvested, so the next turn
	// re-parses only its own appended rows.
	LastOffset int64 `json:"lastOffset"`
}

// MarkCorrection stashes a correction cue from UserPromptSubmit for the next Stop.
func (s *State) MarkCorrection() { s.CorrectionPending = true }

// GateInput is what the daemon hands the gate after draining the tailer on Stop:
// the turn's write-class touched files (re-parsed from raw JSONL) and the turn's
// token delta. The stashed correction cue lives on State.
type GateInput struct {
	FilesTouched []string
	TurnTokens   int64
}

// GateDecision is the gate's verdict on whether to spend a judge call, plus the
// reason (for diagnostics / tests).
type GateDecision struct {
	Fire   bool
	Reason string
}

// Gate decides whether a finished turn warrants a judge call. It fires if ANY of:
// (a) the file-path Jaccard of the turn's touched set vs the current segment's set
// is below the floor (a lexical topic shift); (b) the stashed correction cue; or
// (c) the cadence floor (>= cadenceTurns turns OR >= cadenceTokens tokens since
// the last judge call). The per-session call cap is enforced FIRST: once spent,
// the gate never fires again regardless of the cues (D3). It mutates the cadence
// counters (turns/tokens accumulate) but NOT the segment baseline — that rolls in
// Fold so a non-firing turn still contributes its files to the segment.
func (s *State) Gate(in GateInput) GateDecision {
	s.TurnsSinceJudge++
	s.TokensSinceJudge += in.TurnTokens
	if s.JudgeCalls >= maxJudgeCalls {
		return GateDecision{Fire: false, Reason: "cap"}
	}
	if s.CorrectionPending {
		return GateDecision{Fire: true, Reason: "correction"}
	}
	// Jaccard shift: only meaningful once the segment has a baseline AND this turn
	// actually touched files. A no-edit turn falls through to the cadence floor.
	if len(in.FilesTouched) > 0 && len(s.SegmentFiles) > 0 {
		if jaccard(in.FilesTouched, s.SegmentFiles) < jaccardFloor {
			return GateDecision{Fire: true, Reason: "divergence"}
		}
	}
	if s.TurnsSinceJudge >= cadenceTurns || s.TokensSinceJudge >= cadenceTokens {
		return GateDecision{Fire: true, Reason: "cadence"}
	}
	return GateDecision{Fire: false, Reason: "below-floor"}
}

// jaccard is the intersection-over-union of two string sets. Empty/empty is 1
// (identical), one-empty is 0 (maximal shift).
func jaccard(a, b []string) float64 {
	if len(a) == 0 && len(b) == 0 {
		return 1
	}
	sa := toSet(a)
	sb := toSet(b)
	inter := 0
	for k := range sa {
		if sb[k] {
			inter++
		}
	}
	union := len(sa) + len(sb) - inter
	if union == 0 {
		return 1
	}
	return float64(inter) / float64(union)
}

func toSet(xs []string) map[string]bool {
	m := make(map[string]bool, len(xs))
	for _, x := range xs {
		if x != "" {
			m[x] = true
		}
	}
	return m
}

// mergeFiles folds the turn's touched files into the segment baseline, preserving
// first-seen order and de-duplicating.
func mergeFiles(base, add []string) []string {
	seen := toSet(base)
	out := append([]string(nil), base...)
	for _, f := range add {
		if f != "" && !seen[f] {
			seen[f] = true
			out = append(out, f)
		}
	}
	return out
}
