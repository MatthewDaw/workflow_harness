package desktop

import (
	"encoding/base64"
	"sync"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/daemon"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// fakeClient implements attachClient for tests.
type fakeClient struct {
	mu       sync.Mutex
	out      func(sessID string, b []byte)
	onSess   func([]daemon.SessInfo)
	onEvent  func(event.Envelope)
	onStatus func(daemon.StatusSnapshot)
	inputs   [][]byte
	focuses  []string
	newCount int
	renames  [][2]string
	resizes  [][2]int
	closes   []string
	shutdowns int
	sessions []daemon.SessInfo
}

func (f *fakeClient) SetHandlers(out func(string, []byte), onSess func([]daemon.SessInfo)) {
	f.out, f.onSess = out, onSess
}
func (f *fakeClient) SetEventHandler(fn func(event.Envelope))           { f.onEvent = fn }
func (f *fakeClient) SetStatusHandler(fn func(daemon.StatusSnapshot)) { f.onStatus = fn }
func (f *fakeClient) Input(b []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.inputs = append(f.inputs, append([]byte(nil), b...))
	return nil
}
func (f *fakeClient) Resize(c, r int) error             { f.resizes = append(f.resizes, [2]int{c, r}); return nil }
func (f *fakeClient) Focus(id string) error             { f.focuses = append(f.focuses, id); return nil }
func (f *fakeClient) NewSession() error           { f.newCount++; return nil }
func (f *fakeClient) Rename(id, name string) error      { f.renames = append(f.renames, [2]string{id, name}); return nil }
func (f *fakeClient) CloseSession(id string) error      { f.closes = append(f.closes, id); return nil }
func (f *fakeClient) Shutdown() error                   { f.shutdowns++; return nil }
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

func TestBridgeCloseSessionDelegates(t *testing.T) {
	fc := &fakeClient{}
	b := New(fc, &fakeEmitter{})
	if err := b.CloseSession("sess-9"); err != nil {
		t.Fatal(err)
	}
	if len(fc.closes) != 1 || fc.closes[0] != "sess-9" {
		t.Fatalf("closes = %v, want [sess-9]", fc.closes)
	}
}

func TestBridgeShutdownDelegates(t *testing.T) {
	fc := &fakeClient{}
	b := New(fc, &fakeEmitter{})
	if err := b.Shutdown(); err != nil {
		t.Fatal(err)
	}
	if fc.shutdowns != 1 {
		t.Fatalf("shutdowns = %d, want 1", fc.shutdowns)
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

func TestBridgeForwardsEventsToStream(t *testing.T) {
	fc := &fakeClient{}
	em := &fakeEmitter{}
	b := New(fc, em)
	b.Start()
	fc.onEvent(event.Envelope{V: 1, InstanceID: "i", Host: "h", Event: event.SessionRename("s", "alpha")})

	em.mu.Lock()
	defer em.mu.Unlock()
	if len(em.events) != 1 || em.events[0].name != EventStream {
		t.Fatalf("events = %+v", em.events)
	}
}

func TestBridgeForwardsStatus(t *testing.T) {
	fc := &fakeClient{}
	em := &fakeEmitter{}
	b := New(fc, em)
	b.Start()
	fc.onStatus(daemon.StatusSnapshot{Tokens: 42, CostUSD: 1.5, Drift: 2})

	em.mu.Lock()
	defer em.mu.Unlock()
	if len(em.events) != 1 || em.events[0].name != EventStatus {
		t.Fatalf("events = %+v", em.events)
	}
}
