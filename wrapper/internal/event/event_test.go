package event

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// goldenPath locates the shared golden fixture relative to the wrapper module.
func goldenPath(t *testing.T) string {
	t.Helper()
	// wrapper/internal/event -> repo root is three levels up.
	p := filepath.Join("..", "..", "..", "packages", "shared", "test", "golden", "event-envelope.json")
	if _, err := os.Stat(p); err != nil {
		t.Skipf("golden fixture not found at %s: %v", p, err)
	}
	return p
}

// TestGoldenRoundTrip parses the shared golden fixture, re-serializes it, and
// asserts the JSON is semantically identical. This is the cross-language
// contract guard (U2/U14).
func TestGoldenRoundTrip(t *testing.T) {
	b, err := os.ReadFile(goldenPath(t))
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}

	var envs []Envelope
	if err := json.Unmarshal(b, &envs); err != nil {
		t.Fatalf("unmarshal golden: %v", err)
	}
	if len(envs) != 11 {
		t.Fatalf("expected 11 envelopes in golden, got %d", len(envs))
	}

	for i, env := range envs {
		if err := env.Validate(); err != nil {
			t.Errorf("envelope %d invalid: %v", i, err)
		}
	}

	// Round-trip: re-marshal each envelope and compare the decoded generic maps.
	var want []map[string]any
	if err := json.Unmarshal(b, &want); err != nil {
		t.Fatalf("unmarshal golden to maps: %v", err)
	}
	for i, env := range envs {
		out, err := env.Marshal()
		if err != nil {
			t.Fatalf("marshal envelope %d: %v", i, err)
		}
		var got map[string]any
		if err := json.Unmarshal(out, &got); err != nil {
			t.Fatalf("unmarshal re-marshaled %d: %v", i, err)
		}
		if !reflect.DeepEqual(got, want[i]) {
			t.Errorf("envelope %d round-trip mismatch:\n got=%v\nwant=%v", i, got, want[i])
		}
	}
}

func TestConstructorsValidate(t *testing.T) {
	events := []Event{
		SessionStart("a91f", "weekly-compass", "matt@mbp", "reconcile-variance", "builder", "acme/weekly-compass"),
		SessionRename("a91f", "reconcile-variance"),
		UserMsg("a91f", 120),
		AssistantMsg("a91f", 84),
		ToolCall("a91f", "Read", "src/state/weeklyLifecycle.ts"),
		ToolResult("a91f", true, 8, "142 lines"),
		CostTick("a91f", 0.04, 0.62, 48000),
		StatusChange("a91f", StatusActive, StatusNeedsInput),
		SessionHeartbeat("a91f"),
		SessionTopic("a91f", "a91f-2", "reconcile-variance", "Reconciling the weekly variance rollup."),
		SessionLearning("a91f", "a91f-2", "reconcile-variance", "impl", "Zero the opening balance before summing carryover.", "", "a91f-t7"),
	}
	for _, e := range events {
		if err := e.Validate(); err != nil {
			t.Errorf("%s should be valid: %v", e.Kind, err)
		}
	}
}

// TestSessionStartRepoField verifies the optional repo field is carried on the
// JSON wire under the "repo" key when set, and omitted when empty (so older
// daemons that pass "" stay byte-compatible with pre-repo envelopes).
func TestSessionStartRepoField(t *testing.T) {
	withRepo := SessionStart("a91f", "weekly-compass", "h", "n", "", "acme/weekly-compass")
	b, err := json.Marshal(withRepo)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := m["repo"]; got != "acme/weekly-compass" {
		t.Errorf("repo = %v, want acme/weekly-compass", got)
	}

	withoutRepo := SessionStart("a91f", "weekly-compass", "h", "n", "", "")
	b, err = json.Marshal(withoutRepo)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m2 map[string]any
	if err := json.Unmarshal(b, &m2); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if _, present := m2["repo"]; present {
		t.Errorf("repo key should be omitted when empty, got %v", m2["repo"])
	}
}

func TestRejectsUnknownKind(t *testing.T) {
	e := Event{Kind: "bogus.kind", SessionID: "x"}
	if err := e.Validate(); err == nil {
		t.Error("expected unknown kind to be rejected")
	}
}

func TestRejectsBadEnvelope(t *testing.T) {
	cases := []Envelope{
		{V: 2, InstanceID: "i", Host: "h", Event: UserMsg("s", 1)},                 // bad version
		{V: 1, InstanceID: "", Host: "h", Event: UserMsg("s", 1)},                  // missing instanceId
		{V: 1, InstanceID: "i", Host: "h", Seq: -1, Event: UserMsg("s", 1)},        // negative seq
		{V: 1, InstanceID: "i", Host: "h", Event: StatusChange("s", "weird", "x")}, // bad status
	}
	for i, env := range cases {
		if err := env.Validate(); err == nil {
			t.Errorf("case %d should be invalid", i)
		}
	}
}
