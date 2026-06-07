package daemon

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/capture"
	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/judge"
	"github.com/workflow-harness/claude-plus/internal/topic"
)

// stubJudge swaps the runJudge seam for the duration of a test so the topic gate
// folds a deterministic verdict instead of spawning a headless model. It also
// captures the inputs the gate built, so a test can assert the gate did/did not
// call the judge.
func stubJudge(t *testing.T, verdict topic.Verdict, failed bool) (calls func() int, lastInput func() judge.Input) {
	t.Helper()
	var n int
	var last judge.Input
	prev := runJudge
	runJudge = func(in judge.Input) (topic.Verdict, bool) {
		n++
		last = in
		return verdict, failed
	}
	t.Cleanup(func() { runJudge = prev })
	return func() int { return n }, func() judge.Input { return last }
}

// seedTranscript writes a raw JSONL transcript at the tab's transcript path so the
// gate's HarvestTurn re-parse has rows to read.
func seedTranscript(t *testing.T, repo, tabID, content string) {
	t.Helper()
	p, err := capture.TranscriptPath(repo, tabID)
	if err != nil {
		t.Fatalf("TranscriptPath: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatalf("MkdirAll: %v", err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatalf("WriteFile transcript: %v", err)
	}
}

func editRow(file string) string {
	b, _ := json.Marshal(map[string]any{
		"type": "assistant",
		"message": map[string]any{
			"role": "assistant",
			"content": []any{
				map[string]any{"type": "tool_use", "name": "Edit", "input": map[string]any{"file_path": file}},
			},
		},
	})
	return string(b) + "\n"
}

func userRow(text string) string {
	b, _ := json.Marshal(map[string]any{
		"type":    "user",
		"message": map[string]any{"role": "user", "content": text},
	})
	return string(b) + "\n"
}

func sendStop(d *Daemon, tabID string) {
	hook := capture.HookEvent{HookEventName: "Stop", SessionID: tabID}
	b, _ := json.Marshal(hook)
	d.ingestHook(string(b))
}

func sendPrompt(d *Daemon, tabID, prompt string) {
	hook := capture.HookEvent{HookEventName: "UserPromptSubmit", SessionID: tabID, Prompt: prompt}
	b, _ := json.Marshal(hook)
	d.ingestHook(string(b))
}

// TestStopStillEmitsIdle is the characterization assertion: wiring the topic gate
// into the Stop path must NOT remove the existing status.change -> idle that
// ingestHook emits. The topic gate is a side channel.
func TestStopStillEmitsIdle(t *testing.T) {
	d, _, _ := newPartARuntime(t, partALongSpawn)
	s, err := d.Mux().Spawn("victim")
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	events := collectEvents(d, "stop-idle")

	sendStop(d, s.ID)

	waitForCond(t, func() bool {
		for _, e := range events() {
			if e.Kind == event.KindStatusChange && e.SessionID == s.ID && e.To == event.StatusIdle {
				return true
			}
		}
		return false
	})
}

// TestGateFiresAndEmitsTopicAndLearning: a correction prompt + a Stop drives the
// gate to fire, the stubbed verdict folds into a session.topic plus exactly one
// impl session.learning, both emitted through the daemon bus.
func TestGateFiresAndEmitsTopicAndLearning(t *testing.T) {
	calls, _ := stubJudge(t, topic.Verdict{
		SameTopic:    true,
		TopicLabel:   "login-redirect",
		Description:  "fixing the post-login redirect",
		IsCorrection: true,
		ImplLearning: "redirect to /home after login",
	}, false)

	d, _, repo := newPartARuntime(t, partALongSpawn)
	s, err := d.Mux().Spawn("victim")
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	seedTranscript(t, repo, s.ID, userRow("no, redirect to /home")+editRow("src/login.tsx"))

	events := collectEvents(d, "gate-fire")

	// A correction-smelling prompt stashes the cue; the Stop fires the gate.
	sendPrompt(d, s.ID, "no, redirect to /home instead")
	sendStop(d, s.ID)

	var sawTopic, sawImpl bool
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		for _, e := range events() {
			if e.SessionID != s.ID {
				continue
			}
			if e.Kind == event.KindSessionTopic && e.TopicLabel == "login-redirect" {
				sawTopic = true
			}
			if e.Kind == event.KindSessionLearning && e.Stream == "impl" {
				sawImpl = true
			}
		}
		if sawTopic && sawImpl {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !sawTopic {
		t.Error("expected a session.topic emit after a firing gate")
	}
	if !sawImpl {
		t.Error("expected an impl session.learning after a correction")
	}
	if calls() == 0 {
		t.Error("the judge should have been called when the gate fired")
	}
}

// TestGateDoesNotFireOnPlainTurn: a non-correction turn with no file divergence
// and below the cadence floor must NOT call the judge (assert the mock was not
// called) and emit no topic/learning.
func TestGateDoesNotFireOnPlainTurn(t *testing.T) {
	calls, _ := stubJudge(t, topic.Verdict{SameTopic: true}, false)

	d, _, repo := newPartARuntime(t, partALongSpawn)
	s, err := d.Mux().Spawn("victim")
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	// A plain, non-correction prompt; no prior segment baseline → no divergence; a
	// single turn is below the 3-turn cadence floor.
	seedTranscript(t, repo, s.ID, userRow("add a small helper function")+editRow("src/util.go"))

	events := collectEvents(d, "no-fire")
	sendPrompt(d, s.ID, "add a small helper function")
	sendStop(d, s.ID)

	// Give the loop time to drain + run the gate.
	time.Sleep(400 * time.Millisecond)

	if calls() != 0 {
		t.Errorf("judge must NOT be called on a plain below-floor turn, calls=%d", calls())
	}
	for _, e := range events() {
		if e.Kind == event.KindSessionTopic || e.Kind == event.KindSessionLearning {
			t.Errorf("no topic/learning should be emitted when the gate does not fire, saw %s", e.Kind)
		}
	}
}

// TestParseFailedEmitsNothing: a parseFailed verdict keeps the current topic and
// emits neither a session.topic nor a session.learning (observable-but-silent).
func TestParseFailedEmitsNothing(t *testing.T) {
	stubJudge(t, topic.Verdict{SameTopic: true}, true) // parseFailed=true

	d, _, repo := newPartARuntime(t, partALongSpawn)
	s, err := d.Mux().Spawn("victim")
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	seedTranscript(t, repo, s.ID, userRow("no, that is wrong")+editRow("src/login.tsx"))

	events := collectEvents(d, "parsefail")
	sendPrompt(d, s.ID, "no, that is wrong, fix it")
	sendStop(d, s.ID)

	time.Sleep(400 * time.Millisecond)
	for _, e := range events() {
		if e.SessionID == s.ID && (e.Kind == event.KindSessionTopic || e.Kind == event.KindSessionLearning) {
			t.Errorf("parseFailed must emit no topic/learning, saw %s", e.Kind)
		}
	}
}
