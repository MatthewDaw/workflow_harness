package desktop

import (
	"encoding/base64"
	"sync"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/daemon"
)

// fakeClient implements attachClient for tests.
type fakeClient struct {
	mu       sync.Mutex
	out      func(sessID string, b []byte)
	onSess   func([]daemon.SessInfo)
	inputs   [][]byte
	focuses  []string
	newCount int
	resizes  [][2]int
	sessions []daemon.SessInfo
}

func (f *fakeClient) SetHandlers(out func(string, []byte), onSess func([]daemon.SessInfo)) {
	f.out, f.onSess = out, onSess
}
func (f *fakeClient) Input(b []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.inputs = append(f.inputs, append([]byte(nil), b...))
	return nil
}
func (f *fakeClient) Resize(c, r int) error             { f.resizes = append(f.resizes, [2]int{c, r}); return nil }
func (f *fakeClient) Focus(id string) error             { f.focuses = append(f.focuses, id); return nil }
func (f *fakeClient) NewSession(string) error           { f.newCount++; return nil }
func (f *fakeClient) Detach() error                     { return nil }
func (f *fakeClient) Run() error                        { return nil }
func (f *fakeClient) InitialSessions() []daemon.SessInfo { return f.sessions }

// fakeEmitter records events emitted to the webview.
type fakeEmitter struct {
	mu     sync.Mutex
	events []emitted
}
type emitted struct {
	name string
	data []interface{}
}

func (e *fakeEmitter) Emit(name string, data ...interface{}) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.events = append(e.events, emitted{name, data})
}

func TestBridgeEmitsOutputAsBase64(t *testing.T) {
	fc := &fakeClient{}
	em := &fakeEmitter{}
	b := New(fc, em)
	b.Start()

	fc.out("sess-1", []byte("hello\x1b[0m"))

	em.mu.Lock()
	defer em.mu.Unlock()
	if len(em.events) != 1 {
		t.Fatalf("want 1 event, got %d", len(em.events))
	}
	ev := em.events[0]
	if ev.name != EventOutput {
		t.Fatalf("want %q, got %q", EventOutput, ev.name)
	}
	payload, ok := ev.data[0].(OutputEvent)
	if !ok {
		t.Fatalf("want OutputEvent, got %T", ev.data[0])
	}
	if payload.SessID != "sess-1" {
		t.Errorf("sessID = %q", payload.SessID)
	}
	want := base64.StdEncoding.EncodeToString([]byte("hello\x1b[0m"))
	if payload.DataB64 != want {
		t.Errorf("data = %q, want %q", payload.DataB64, want)
	}
}

func TestBridgeSendInputDecodesToBytes(t *testing.T) {
	fc := &fakeClient{}
	b := New(fc, &fakeEmitter{})
	if err := b.SendInput("ls\n"); err != nil {
		t.Fatal(err)
	}
	if len(fc.inputs) != 1 || string(fc.inputs[0]) != "ls\n" {
		t.Fatalf("inputs = %v", fc.inputs)
	}
}

func TestBridgeListSessionsReturnsInitial(t *testing.T) {
	fc := &fakeClient{sessions: []daemon.SessInfo{{ID: "a", Name: "alpha"}}}
	b := New(fc, &fakeEmitter{})
	got := b.ListSessions()
	if len(got) != 1 || got[0].ID != "a" {
		t.Fatalf("ListSessions = %v", got)
	}
}

func TestBridgeEmitsSessionsOnUpdate(t *testing.T) {
	fc := &fakeClient{}
	em := &fakeEmitter{}
	b := New(fc, em)
	b.Start()
	fc.onSess([]daemon.SessInfo{{ID: "x", Name: "ex", Focused: true}})

	em.mu.Lock()
	defer em.mu.Unlock()
	if len(em.events) != 1 || em.events[0].name != EventSessions {
		t.Fatalf("events = %+v", em.events)
	}
}
