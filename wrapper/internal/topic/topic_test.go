package topic

import (
	"strings"
	"testing"
)

// --- Gate firing tests -------------------------------------------------------

// TestGateFiresOnFileDivergence: a turn whose touched-file set barely overlaps
// the current segment's set (Jaccard < τ) fires the gate.
func TestGateFiresOnFileDivergence(t *testing.T) {
	s := &State{SegmentFiles: []string{"a.go", "b.go", "c.go"}}
	d := s.Gate(GateInput{FilesTouched: []string{"x.go", "y.go"}})
	if !d.Fire {
		t.Fatalf("gate should fire on file divergence, got %+v", d)
	}
	if d.Reason != "divergence" {
		t.Errorf("reason = %q, want divergence", d.Reason)
	}
}

// TestGateFiresOnCorrectionCue: the stashed correction cue fires the gate even
// when the touched files are identical (R2: cue is independent of file churn).
func TestGateFiresOnCorrectionCue(t *testing.T) {
	s := &State{SegmentFiles: []string{"a.go"}, CorrectionPending: true}
	d := s.Gate(GateInput{FilesTouched: []string{"a.go"}}) // same file, no divergence
	if !d.Fire || d.Reason != "correction" {
		t.Fatalf("gate should fire on correction cue, got %+v", d)
	}
}

// TestGateFiresOnCadence: with no divergence and no cue, the cadence floor fires
// once enough turns accumulate.
func TestGateFiresOnCadence(t *testing.T) {
	s := &State{SegmentFiles: []string{"a.go"}}
	// Two same-file turns: below the 3-turn floor, no fire.
	if d := s.Gate(GateInput{FilesTouched: []string{"a.go"}}); d.Fire {
		t.Fatalf("turn 1 should not fire, got %+v", d)
	}
	if d := s.Gate(GateInput{FilesTouched: []string{"a.go"}}); d.Fire {
		t.Fatalf("turn 2 should not fire, got %+v", d)
	}
	// Third turn hits the cadence floor.
	d := s.Gate(GateInput{FilesTouched: []string{"a.go"}})
	if !d.Fire || d.Reason != "cadence" {
		t.Fatalf("turn 3 should fire on cadence, got %+v", d)
	}
}

// TestGateDoesNotFireBelowFloor: identical files, no cue, below the cadence floor
// → no judge call.
func TestGateDoesNotFireBelowFloor(t *testing.T) {
	s := &State{SegmentFiles: []string{"a.go", "b.go"}}
	d := s.Gate(GateInput{FilesTouched: []string{"a.go", "b.go"}, TurnTokens: 10})
	if d.Fire {
		t.Fatalf("gate should NOT fire (identical files, below floor), got %+v", d)
	}
}

// TestGateTokenCadence: a single large-token turn trips the token floor.
func TestGateTokenCadence(t *testing.T) {
	s := &State{SegmentFiles: []string{"a.go"}}
	d := s.Gate(GateInput{FilesTouched: []string{"a.go"}, TurnTokens: cadenceTokens + 1})
	if !d.Fire || d.Reason != "cadence" {
		t.Fatalf("a >1500-token turn should fire on cadence, got %+v", d)
	}
}

// TestGatePerSessionCap: once maxJudgeCalls is reached the gate never fires again
// even with a correction cue (D3 cap).
func TestGatePerSessionCap(t *testing.T) {
	s := &State{JudgeCalls: maxJudgeCalls, CorrectionPending: true,
		SegmentFiles: []string{"a.go"}}
	d := s.Gate(GateInput{FilesTouched: []string{"z.go"}})
	if d.Fire {
		t.Fatalf("gate must not fire once per-session cap reached, got %+v", d)
	}
	if d.Reason != "cap" {
		t.Errorf("reason = %q, want cap", d.Reason)
	}
}

// --- Correction cue ----------------------------------------------------------

func TestSmellsLikeCorrection(t *testing.T) {
	cases := map[string]bool{
		"no, that should redirect to /home":   true,
		"actually use tabs not spaces":         true,
		"you forgot the error handling":        true,
		"revert that change":                   true,
		"build a login page":                   false,
		"add a new endpoint for billing":       false,
		"":                                     false,
	}
	for prompt, want := range cases {
		if got := SmellsLikeCorrection(prompt); got != want {
			t.Errorf("SmellsLikeCorrection(%q) = %v, want %v", prompt, got, want)
		}
	}
}

// --- Fold: learning routing --------------------------------------------------

// TestFoldCorrectionYieldsOneImplLearning: a correction with only an
// impl_learning yields exactly one impl learning, no doc learning.
func TestFoldCorrectionYieldsOneImplLearning(t *testing.T) {
	s := &State{}
	res := s.Fold(Verdict{
		SameTopic:    true,
		TopicLabel:   "login",
		Description:  "building login",
		IsCorrection: true,
		ImplLearning: "redirect to /home after login",
	}, []string{"login.tsx"}, "")
	if len(res.Learnings) != 1 {
		t.Fatalf("want exactly 1 learning, got %d: %+v", len(res.Learnings), res.Learnings)
	}
	if res.Learnings[0].Stream != "impl" {
		t.Errorf("stream = %q, want impl", res.Learnings[0].Stream)
	}
}

// TestFoldContradictionYieldsImplAndDoc: ContradictsDoc with a doc_question
// yields impl + doc (two learnings); the doc carries the docRef.
func TestFoldContradictionYieldsImplAndDoc(t *testing.T) {
	s := &State{}
	res := s.Fold(Verdict{
		SameTopic:      true,
		TopicLabel:     "login",
		Description:    "building login",
		IsCorrection:   true,
		ContradictsDoc: true,
		ImplLearning:   "redirect to /home after login",
		DocQuestion:    "where should login redirect?",
	}, []string{"login.tsx"}, "docs/plans/features/login.html")
	if len(res.Learnings) != 2 {
		t.Fatalf("want impl + doc, got %d: %+v", len(res.Learnings), res.Learnings)
	}
	var impl, doc *Learning
	for i := range res.Learnings {
		switch res.Learnings[i].Stream {
		case "impl":
			impl = &res.Learnings[i]
		case "doc":
			doc = &res.Learnings[i]
		}
	}
	if impl == nil || doc == nil {
		t.Fatalf("missing a stream: %+v", res.Learnings)
	}
	if doc.DocRef != "docs/plans/features/login.html" {
		t.Errorf("doc learning docRef = %q", doc.DocRef)
	}
}

// TestFoldDocsOptional: with no doc loaded (ContradictsDoc=false) only the impl
// learning flows, never a doc learning (R6).
func TestFoldDocsOptional(t *testing.T) {
	s := &State{}
	res := s.Fold(Verdict{
		SameTopic:      true,
		IsCorrection:   true,
		ContradictsDoc: false,
		ImplLearning:   "use the repo logger, not fmt.Println",
		DocQuestion:    "should not appear", // ignored when ContradictsDoc=false
	}, nil, "")
	if len(res.Learnings) != 1 || res.Learnings[0].Stream != "impl" {
		t.Fatalf("docs-optional: want exactly one impl learning, got %+v", res.Learnings)
	}
}

// --- Fold: thrash guard + debounce -------------------------------------------

// TestFoldThrashGuardSlugUnchanged: the topic/description/segment evolve across
// many folds but the caller's slug is never touched here (Fold returns no name
// field) and a same_topic:false is debounced (held one turn) before a new segment
// opens.
func TestFoldDebounceAndSegments(t *testing.T) {
	s := &State{}
	// First fold (SegmentID empty) opens segment 1 immediately.
	r1 := s.Fold(Verdict{SameTopic: true, TopicLabel: "a", Description: "d1"}, nil, "")
	seg1 := r1.SegmentID
	if seg1 == "" {
		t.Fatal("first fold should open a segment")
	}
	// A same_topic:false is HELD (debounced) — segment unchanged this turn.
	r2 := s.Fold(Verdict{SameTopic: false, TopicLabel: "b", Description: "d2"}, nil, "")
	if r2.SegmentID != seg1 {
		t.Errorf("debounce: segment should not switch on first same_topic:false (%q -> %q)", seg1, r2.SegmentID)
	}
	if !s.PendingSwitch {
		t.Error("a held switch should set PendingSwitch")
	}
	// A second same_topic:false confirms the switch — new segment.
	r3 := s.Fold(Verdict{SameTopic: false, TopicLabel: "b", Description: "d3"}, nil, "")
	if r3.SegmentID == seg1 {
		t.Errorf("confirmed switch should open a NEW segment, still %q", r3.SegmentID)
	}
}

// TestFoldTurnIDMonotonic: each fold advances a stable per-session turn id so a
// re-emit dedupes at ingest.
func TestFoldTurnIDMonotonic(t *testing.T) {
	s := &State{}
	a := s.Fold(Verdict{SameTopic: true}, nil, "").TurnID
	b := s.Fold(Verdict{SameTopic: true}, nil, "").TurnID
	if a == b || a == "" {
		t.Errorf("turn ids should be distinct + non-empty: %q, %q", a, b)
	}
}

// --- Scrub + length caps -----------------------------------------------------

func TestScrubSecrets(t *testing.T) {
	in := "use this token: ghp_ABCDEFGHIJ0123456789abcdefghij and AKIAIOSFODNN7EXAMPLE"
	out := scrubSecrets(in)
	if strings.Contains(out, "ghp_ABCDEFGHIJ") {
		t.Error("GitHub PAT not scrubbed")
	}
	if strings.Contains(out, "AKIAIOSFODNN7EXAMPLE") {
		t.Error("AWS key not scrubbed")
	}
	if !strings.Contains(out, redacted) {
		t.Error("expected a redaction marker")
	}
}

// TestFoldLengthCaps: impl is capped at 500, doc at 300, AFTER scrubbing.
func TestFoldLengthCaps(t *testing.T) {
	s := &State{}
	longImpl := strings.Repeat("x", 800)
	longDoc := strings.Repeat("y", 500)
	res := s.Fold(Verdict{
		SameTopic:      true,
		IsCorrection:   true,
		ContradictsDoc: true,
		ImplLearning:   longImpl,
		DocQuestion:    longDoc,
	}, nil, "d")
	var impl, doc string
	for _, l := range res.Learnings {
		if l.Stream == "impl" {
			impl = l.Text
		} else {
			doc = l.Text
		}
	}
	if len([]rune(impl)) != maxImplLearning {
		t.Errorf("impl len = %d, want %d", len([]rune(impl)), maxImplLearning)
	}
	if len([]rune(doc)) != maxDocQuestion {
		t.Errorf("doc len = %d, want %d", len([]rune(doc)), maxDocQuestion)
	}
}

// --- Checkpoint store --------------------------------------------------------

// TestCheckpointRestoredAfterRestart simulates a daemon restart: state saved to
// the store is reloaded with its carried topic/cadence/turn fields intact.
func TestCheckpointRestoredAfterRestart(t *testing.T) {
	dir := t.TempDir()
	st, err := NewStore(dir)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	want := State{
		TopicLabel: "auth", Description: "building auth", SegmentID: "seg-2",
		SegmentFiles: []string{"a.go", "b.go"}, TurnCounter: 7, JudgeCalls: 3,
		SegmentSeq: 2, LastOffset: 4096,
	}
	if err := st.Save("tab-1", want); err != nil {
		t.Fatalf("Save: %v", err)
	}

	// "Restart": a brand-new store over the same dir reloads the checkpoint.
	st2, err := NewStore(dir)
	if err != nil {
		t.Fatalf("NewStore (restart): %v", err)
	}
	got, ok := st2.Load("tab-1")
	if !ok {
		t.Fatal("checkpoint not restored after restart")
	}
	if got.TopicLabel != want.TopicLabel || got.SegmentID != want.SegmentID ||
		got.TurnCounter != want.TurnCounter || got.LastOffset != want.LastOffset {
		t.Errorf("restored state mismatch: got %+v want %+v", got, want)
	}
	if len(got.SegmentFiles) != 2 {
		t.Errorf("segment files not restored: %+v", got.SegmentFiles)
	}
}

// TestStoreLoadMissing: an unknown session loads a fresh zero state (ok=false).
func TestStoreLoadMissing(t *testing.T) {
	st, _ := NewStore(t.TempDir())
	if _, ok := st.Load("never-seen"); ok {
		t.Error("Load of an unknown session should report ok=false")
	}
}

// --- Limiter -----------------------------------------------------------------

// TestLimiterConcurrencyCap: a 2-permit bucket grants exactly 2 concurrent
// acquisitions and denies the third until a release.
func TestLimiterConcurrencyCap(t *testing.T) {
	l := NewLimiter(2)
	if !l.TryAcquire() || !l.TryAcquire() {
		t.Fatal("first two acquisitions should succeed")
	}
	if l.TryAcquire() {
		t.Fatal("third acquisition should be denied (cap=2)")
	}
	l.Release()
	if !l.TryAcquire() {
		t.Fatal("acquisition after a release should succeed")
	}
}
