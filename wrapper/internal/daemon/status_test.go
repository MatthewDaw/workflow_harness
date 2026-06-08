package daemon

import (
	"testing"

	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/event"
)

func TestStatusAggregatesMessageTokens(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	d.PublishEvent(envWith("s", event.UserMsg("s", 100)))
	d.PublishEvent(envWith("s", event.AssistantMsg("s", 50)))
	st := d.Status()
	if st.Tokens != 150 {
		t.Errorf("tokens = %d, want 150", st.Tokens)
	}
}

func TestStatusIgnoresNonMessageEvents(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	d.PublishEvent(envWith("s", event.ToolCall("s", "Read", "file.go")))
	st := d.Status()
	if st.Tokens != 0 {
		t.Errorf("non-message event affected meter: %+v", st)
	}
}

func TestSetDrift(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	d.SetDrift(3)
	if got := d.Status().Drift; got != 3 {
		t.Errorf("drift = %d, want 3", got)
	}
}

// staticRemote is a minimal config.RemoteSource yielding a fixed effective set,
// used to prove SyncConfigOnce folds drift into the status meter (U19).
type staticRemote struct{ items []config.RemoteItem }

func (s staticRemote) Fetch() ([]config.RemoteItem, error)    { return s.items, nil }
func (s staticRemote) Body(config.RemoteItem) (string, error) { return "", nil }
func (s staticRemote) Push(config.Item, string) error         { return nil }
func (s staticRemote) AgentSkills(string) []string            { return nil }

// TestSyncConfigOnceSetsDrift proves the drift meter reflects real HQ drift: with
// an HQ-only item and no matching local definition, the snapshot's Drift is 1.
func TestSyncConfigOnceSetsDrift(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if d.Status().Drift != 0 {
		t.Fatalf("fresh daemon drift = %d, want 0", d.Status().Drift)
	}

	src := staticRemote{items: []config.RemoteItem{{Kind: config.KindAgent, Name: "hq-only", Hash: "h"}}}
	if err := d.SyncConfigOnce(src); err != nil {
		t.Fatalf("SyncConfigOnce: %v", err)
	}
	if got := d.Status().Drift; got != 1 {
		t.Fatalf("drift after sync = %d, want 1 (one HQ-only item)", got)
	}
}
