# claude+ Desktop — Phase 1: Session-core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable cross-platform desktop window that attaches to the existing per-repo `claude+` daemon and drives a live `claude` session — xterm.js live pane + a clickable session-list panel — launched via `claude+ --gui`.

**Architecture:** A Wails v2 app (`cmd/claude-plus-desktop`) lives *inside the existing `wrapper/` Go module*, so it links `internal/daemon` directly and a protocol change breaks its build. A thin Go **bridge** (`internal/desktop`) wraps the existing `daemon.Client` attach loop: daemon → bridge → Wails runtime events → webview (xterm.js); webview → bound Go methods → bridge → daemon. PTY output is *pushed* via `EventsEmit` (a continuous stream), not bound-method returns; bound methods carry request/response control (input, resize, focus, new, list). This phase keeps the daemon's existing single-focus behavior — concurrent per-client attach is Phase 2.

**Tech Stack:** Go 1.26, Wails v2 (WebView2 / WKWebView / WebKitGTK), React + TypeScript + Vite (Wails default), xterm.js (`@xterm/xterm` + `@xterm/addon-fit`).

**Source of truth for the daemon client API (read before starting):**
- `wrapper/internal/daemon/client.go` — `Dial(repoRoot) (*Client, error)`; fields `Out func(sessID string, b []byte)`, `OnSessions func([]SessInfo)`, `Sessions []SessInfo`; methods `Input([]byte)`, `Resize(cols,rows int)`, `Focus(sessID string)`, `NewSession(ticket string)`, `Detach()`, `Run() error`.
- `wrapper/internal/daemon/attach.go` — `SessInfo{ID, Name string; Focused bool; Status string}`.
- `wrapper/cmd/claude-plus/main.go` — `daemon.EnsureDaemon(repo)`, `resolveRepoRoot()`, the `--gui` flag will be added here.

**Scope boundaries (Phase 1 non-goals):** no event-stream/agents/forge/tickets panels; no `FrameEvent`/`FrameStatus`/`FrameAgentDiff`; no per-client focus/PTY sizing (Phase 2); no code signing; no CI matrix (added in a later phase). Single active client semantics are inherited from today's daemon.

---

## File Structure

| Path | Responsibility | New/Modified |
|---|---|---|
| `wrapper/internal/desktop/bridge.go` | Wraps the attach client; converts daemon callbacks → emitter events; exposes control methods | Create |
| `wrapper/internal/desktop/bridge_test.go` | Unit tests for the bridge against a fake attach client + fake emitter | Create |
| `wrapper/cmd/claude-plus-desktop/main.go` | Wails entry; constructs the app, registers the bridge-backed `App` | Create |
| `wrapper/cmd/claude-plus-desktop/app.go` | Wails `App` struct: bound methods + startup wiring of the bridge to the Wails runtime emitter | Create |
| `wrapper/cmd/claude-plus-desktop/wails.json` | Wails project config | Create (scaffold) |
| `wrapper/cmd/claude-plus-desktop/frontend/**` | React+TS app: xterm.js pane + session-list panel | Create (scaffold + edits) |
| `wrapper/cmd/claude-plus/main.go` | Add `--gui`: discover + exec the desktop binary | Modify |
| `wrapper/cmd/claude-plus/gui.go` | `--gui` discovery/launch helper + its test seam | Create |
| `wrapper/cmd/claude-plus/gui_test.go` | Unit test for desktop-binary discovery | Create |

---

## Task 1: Install and verify the Wails toolchain

**Files:** none (environment setup).

- [ ] **Step 1: Install the Wails CLI**

Run:
```bash
go install github.com/wailsapp/wails/v2/cmd/wails@v2.10.1
```
(Pin a known v2 release; do not use `@latest` so the plan is reproducible.)

- [ ] **Step 2: Verify the toolchain and platform deps**

Run:
```bash
wails doctor
```
Expected: reports Go, npm/node found, and the platform webview present (WebView2 on Windows, WebKitGTK on Linux, WKWebView on macOS). If WebView2 is missing on Windows, install the Evergreen runtime; if WebKitGTK is missing on Linux, install `libwebkit2gtk-4.0-dev` (or distro equivalent). Do not proceed until `wails doctor` is green.

- [ ] **Step 3: Confirm Go can see the existing module**

Run:
```bash
cd wrapper && go build ./... && go test ./internal/daemon/ -run TestAttachDetachReattach -count=1
```
Expected: build succeeds and the existing attach test passes — confirms the baseline tree is healthy before adding the app.

---

## Task 2: Scaffold the Wails app inside the wrapper module

**Files:**
- Create: `wrapper/cmd/claude-plus-desktop/{main.go, app.go, wails.json, frontend/**}`

- [ ] **Step 1: Generate a Wails project in a temp dir**

Run:
```bash
cd /tmp && wails init -n claude-plus-desktop -t react-ts
```
Expected: creates `/tmp/claude-plus-desktop/` with `main.go`, `app.go`, `wails.json`, `frontend/` (Vite + React + TS).

- [ ] **Step 2: Relocate the scaffold under the existing module**

Run:
```bash
mkdir -p wrapper/cmd/claude-plus-desktop
cp -r /tmp/claude-plus-desktop/. wrapper/cmd/claude-plus-desktop/
rm -f wrapper/cmd/claude-plus-desktop/go.mod wrapper/cmd/claude-plus-desktop/go.sum
```
The desktop app must NOT have its own `go.mod` — deleting it makes the package part of the `github.com/workflow-harness/claude-plus` module (that shared-module membership is the compile-time coupling the design requires).

- [ ] **Step 3: Add the Wails dependency to the existing module**

Run:
```bash
cd wrapper && go get github.com/wailsapp/wails/v2@v2.10.1 && go mod tidy
```
Expected: `wails/v2` added to `wrapper/go.mod`.

- [ ] **Step 4: Verify the empty app builds on this OS**

Run:
```bash
cd wrapper/cmd/claude-plus-desktop && wails build
```
Expected: produces a binary under `build/bin/`. Launch it once to confirm an empty window opens, then close it.

- [ ] **Step 5: Commit the scaffold**

```bash
git add wrapper/cmd/claude-plus-desktop wrapper/go.mod wrapper/go.sum
git commit -m "feat(desktop): scaffold Wails app in the wrapper module"
```

---

## Task 3: The Go bridge (testable core)

The bridge is the unit-testable heart of Phase 1. It depends on two small interfaces so tests need no real daemon or Wails runtime.

**Files:**
- Create: `wrapper/internal/desktop/bridge.go`
- Test: `wrapper/internal/desktop/bridge_test.go`

- [ ] **Step 1: Write the failing test**

Create `wrapper/internal/desktop/bridge_test.go`:

```go
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
func (f *fakeClient) Input(b []byte) error    { f.mu.Lock(); defer f.mu.Unlock(); f.inputs = append(f.inputs, append([]byte(nil), b...)); return nil }
func (f *fakeClient) Resize(c, r int) error   { f.resizes = append(f.resizes, [2]int{c, r}); return nil }
func (f *fakeClient) Focus(id string) error   { f.focuses = append(f.focuses, id); return nil }
func (f *fakeClient) NewSession(string) error { f.newCount++; return nil }
func (f *fakeClient) Detach() error           { return nil }
func (f *fakeClient) Run() error              { return nil }
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd wrapper && go test ./internal/desktop/ -run TestBridge -v`
Expected: FAIL to compile — `New`, `EventOutput`, `OutputEvent`, etc. undefined.

- [ ] **Step 3: Write the bridge implementation**

Create `wrapper/internal/desktop/bridge.go`:

```go
// Package desktop adapts the daemon attach client to a GUI frontend: it turns
// daemon push-callbacks (PTY output, session-list updates) into emitter events
// the webview consumes, and exposes control methods the webview calls. It is
// frontend-agnostic and Wails-free so it can be unit-tested without a window.
package desktop

import (
	"encoding/base64"

	"github.com/workflow-harness/claude-plus/internal/daemon"
)

// Event names emitted to the webview. Keep in sync with the frontend listeners.
const (
	EventOutput   = "pty:output"
	EventSessions = "sessions:update"
)

// OutputEvent carries a chunk of a session's PTY output to the webview. Bytes
// are base64-encoded because Wails serializes event payloads as JSON and PTY
// output is arbitrary binary (escape sequences, UTF-8).
type OutputEvent struct {
	SessID  string `json:"sessId"`
	DataB64 string `json:"dataB64"`
}

// attachClient is the subset of the daemon attach client the bridge needs.
// *daemon.Client is adapted to this in clientAdapter (app.go) so tests can fake
// it without a live daemon.
type attachClient interface {
	SetHandlers(out func(sessID string, b []byte), onSessions func([]daemon.SessInfo))
	Input(b []byte) error
	Resize(cols, rows int) error
	Focus(sessID string) error
	NewSession(ticket string) error
	Detach() error
	Run() error
	InitialSessions() []daemon.SessInfo
}

// Emitter pushes named events to the webview. *wailsEmitter (app.go) wraps the
// Wails runtime; fakeEmitter is used in tests.
type Emitter interface {
	Emit(name string, data ...interface{})
}

// Bridge connects one attach client to one webview emitter.
type Bridge struct {
	c  attachClient
	em Emitter
}

// New builds a bridge over an attach client and an emitter.
func New(c attachClient, em Emitter) *Bridge { return &Bridge{c: c, em: em} }

// Start wires the daemon push-callbacks to emitter events. Call once before the
// read loop runs.
func (b *Bridge) Start() {
	b.c.SetHandlers(
		func(sessID string, data []byte) {
			b.em.Emit(EventOutput, OutputEvent{
				SessID:  sessID,
				DataB64: base64.StdEncoding.EncodeToString(data),
			})
		},
		func(list []daemon.SessInfo) {
			b.em.Emit(EventSessions, list)
		},
	)
}

// Run blocks on the attach read loop until the daemon connection closes.
func (b *Bridge) Run() error { return b.c.Run() }

// --- bound methods (called from the webview) ---

// SendInput forwards keystrokes (a UTF-8 string from xterm) to the focused PTY.
func (b *Bridge) SendInput(data string) error { return b.c.Input([]byte(data)) }

// Resize forwards terminal dimensions to the daemon.
func (b *Bridge) Resize(cols, rows int) error { return b.c.Resize(cols, rows) }

// Focus switches the daemon's focused session.
func (b *Bridge) Focus(sessID string) error { return b.c.Focus(sessID) }

// NewSession spawns a session (optionally linked to a ticket; empty in Phase 1).
func (b *Bridge) NewSession(ticket string) error { return b.c.NewSession(ticket) }

// ListSessions returns the current session list (seeded from the attach ack).
func (b *Bridge) ListSessions() []daemon.SessInfo { return b.c.InitialSessions() }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd wrapper && go test ./internal/desktop/ -run TestBridge -v`
Expected: all four tests PASS.

- [ ] **Step 5: Commit**

```bash
git add wrapper/internal/desktop/bridge.go wrapper/internal/desktop/bridge_test.go
git commit -m "feat(desktop): bridge adapting daemon attach client to webview events"
```

---

## Task 4: Wire the bridge into the Wails app

**Files:**
- Modify: `wrapper/cmd/claude-plus-desktop/app.go`
- Modify: `wrapper/cmd/claude-plus-desktop/main.go`

- [ ] **Step 1: Replace `app.go` with the bridge-backed App**

Overwrite `wrapper/cmd/claude-plus-desktop/app.go`:

```go
package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/wailsapp/wails/v2/pkg/runtime"
	"github.com/workflow-harness/claude-plus/internal/daemon"
	"github.com/workflow-harness/claude-plus/internal/desktop"
)

// App is the Wails-bound type. Its exported methods become callable from JS and
// their signatures generate TypeScript bindings.
type App struct {
	ctx    context.Context
	bridge *desktop.Bridge
	err    string // startup error surfaced to the UI, if any
}

func NewApp() *App { return &App{} }

// startup runs after the window exists. It resolves the repo, ensures the
// daemon, dials it, wires the bridge to the Wails runtime emitter, and starts
// the attach read loop in the background.
func (a *App) startup(ctx context.Context) {
	a.ctx = ctx

	repo, err := resolveRepoRoot()
	if err != nil {
		a.err = fmt.Sprintf("resolve repo: %v", err)
		return
	}
	if _, err := daemon.EnsureDaemon(repo); err != nil {
		a.err = fmt.Sprintf("start daemon: %v", err)
		return
	}
	c, err := daemon.Dial(repo)
	if err != nil {
		a.err = fmt.Sprintf("attach: %v", err)
		return
	}

	a.bridge = desktop.New(&clientAdapter{c: c}, &wailsEmitter{ctx: ctx})
	a.bridge.Start()
	go func() { _ = a.bridge.Run() }() // returns on daemon disconnect
}

// StartupError lets the UI render a connect-failure state.
func (a *App) StartupError() string { return a.err }

// The remaining bound methods delegate to the bridge (nil-guarded so the UI
// degrades instead of panicking when startup failed).
func (a *App) SendInput(data string) error {
	if a.bridge == nil {
		return fmt.Errorf("not connected")
	}
	return a.bridge.SendInput(data)
}
func (a *App) Resize(cols, rows int) error {
	if a.bridge == nil {
		return nil
	}
	return a.bridge.Resize(cols, rows)
}
func (a *App) Focus(sessID string) error {
	if a.bridge == nil {
		return fmt.Errorf("not connected")
	}
	return a.bridge.Focus(sessID)
}
func (a *App) NewSession(ticket string) error {
	if a.bridge == nil {
		return fmt.Errorf("not connected")
	}
	return a.bridge.NewSession(ticket)
}
func (a *App) ListSessions() []daemon.SessInfo {
	if a.bridge == nil {
		return nil
	}
	return a.bridge.ListSessions()
}

// wailsEmitter adapts the Wails runtime to desktop.Emitter.
type wailsEmitter struct{ ctx context.Context }

func (w *wailsEmitter) Emit(name string, data ...interface{}) {
	runtime.EventsEmit(w.ctx, name, data...)
}

// clientAdapter adapts *daemon.Client (exported-field callbacks) to the
// desktop.attachClient interface (SetHandlers + InitialSessions).
type clientAdapter struct{ c *daemon.Client }

func (a *clientAdapter) SetHandlers(out func(string, []byte), onSess func([]daemon.SessInfo)) {
	a.c.Out = out
	a.c.OnSessions = onSess
}
func (a *clientAdapter) Input(b []byte) error              { return a.c.Input(b) }
func (a *clientAdapter) Resize(cols, rows int) error       { return a.c.Resize(cols, rows) }
func (a *clientAdapter) Focus(sessID string) error         { return a.c.Focus(sessID) }
func (a *clientAdapter) NewSession(ticket string) error    { return a.c.NewSession(ticket) }
func (a *clientAdapter) Detach() error                     { return a.c.Detach() }
func (a *clientAdapter) Run() error                        { return a.c.Run() }
func (a *clientAdapter) InitialSessions() []daemon.SessInfo { return a.c.Sessions }

// resolveRepoRoot walks up from cwd to the nearest .git dir; falls back to cwd.
// (Mirrors cmd/claude-plus/main.go so the desktop targets the same daemon.)
func resolveRepoRoot() (string, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	dir := cwd
	for {
		if fi, err := os.Stat(filepath.Join(dir, ".git")); err == nil && fi.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return cwd, nil
		}
		dir = parent
	}
}
```

- [ ] **Step 2: Ensure `main.go` registers `App.startup` and binds `App`**

Open `wrapper/cmd/claude-plus-desktop/main.go` (Wails scaffold). Confirm it constructs `app := NewApp()`, sets `OnStartup: app.startup` in `options.App`, and lists `app` in `Bind`. The scaffold already does this for the default `App`; verify the field/method names match the rewritten `app.go` (`NewApp`, `startup`). Adjust if the scaffold used different names.

- [ ] **Step 3: Generate bindings and build**

Run:
```bash
cd wrapper/cmd/claude-plus-desktop && wails build
```
Expected: build succeeds; `frontend/wailsjs/go/main/App.{js,d.ts}` is regenerated with `SendInput`, `Resize`, `Focus`, `NewSession`, `ListSessions`, `StartupError`, and the `daemon.SessInfo` TS type appears under `frontend/wailsjs/go/models.ts`.

- [ ] **Step 4: Commit**

```bash
git add wrapper/cmd/claude-plus-desktop
git commit -m "feat(desktop): wire bridge to Wails app (bound methods + runtime events)"
```

---

## Task 5: Frontend — xterm.js live pane

**Files:**
- Modify: `wrapper/cmd/claude-plus-desktop/frontend/package.json` (deps)
- Create: `wrapper/cmd/claude-plus-desktop/frontend/src/Terminal.tsx`
- Modify: `wrapper/cmd/claude-plus-desktop/frontend/src/App.tsx`

- [ ] **Step 1: Add xterm.js dependencies**

Run:
```bash
cd wrapper/cmd/claude-plus-desktop/frontend
npm install @xterm/xterm@5.5.0 @xterm/addon-fit@0.10.0
```

- [ ] **Step 2: Create the Terminal component**

Create `frontend/src/Terminal.tsx`:

```tsx
import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { EventsOn } from "../wailsjs/runtime/runtime";
import { SendInput, Resize } from "../wailsjs/go/main/App";

// Decode base64 (the bridge's binary-safe PTY payload) to bytes for xterm.
function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export default function Terminal() {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const term = new XTerm({ convertEol: false, fontFamily: "monospace", fontSize: 13 });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current!);
    fit.fit();

    // Keystrokes → daemon PTY.
    term.onData((data) => { void SendInput(data); });

    // PTY output (any session) → xterm. Phase 1 shows the focused session's
    // stream; the daemon only streams focused bytes today.
    const offOut = EventsOn("pty:output", (p: { sessId: string; dataB64: string }) => {
      term.write(b64ToBytes(p.dataB64));
    });

    // Send the initial size, then on every resize (debounced by the browser's
    // resize cadence; ~immediate is fine for v1).
    const sendSize = () => { fit.fit(); void Resize(term.cols, term.rows); };
    sendSize();
    const ro = new ResizeObserver(sendSize);
    ro.observe(hostRef.current!);

    return () => { offOut(); ro.disconnect(); term.dispose(); };
  }, []);

  return <div ref={hostRef} style={{ width: "100%", height: "100%" }} />;
}
```

- [ ] **Step 3: Render the Terminal from App.tsx**

Overwrite `frontend/src/App.tsx`:

```tsx
import Terminal from "./Terminal";

function App() {
  return (
    <div style={{ display: "flex", height: "100vh", margin: 0 }}>
      <main style={{ flex: 1, minWidth: 0, background: "#1e1e1e" }}>
        <Terminal />
      </main>
    </div>
  );
}

export default App;
```

- [ ] **Step 4: Build and smoke-test against a live daemon**

Run (in one shell, from a git repo with the real `claude` on PATH):
```bash
cd wrapper && go build -o /tmp/claude-plus ./cmd/claude-plus && /tmp/claude-plus   # starts a daemon + session; detach with Ctrl-G d
```
Then:
```bash
cd wrapper/cmd/claude-plus-desktop && wails dev
```
Expected: the window shows the live `claude` session; typing reaches claude and output renders; resizing the window reflows the terminal. Close `wails dev`.

- [ ] **Step 5: Commit**

```bash
git add wrapper/cmd/claude-plus-desktop/frontend
git commit -m "feat(desktop): xterm.js live session pane wired to the bridge"
```

---

## Task 6: Frontend — session-list panel

**Files:**
- Create: `wrapper/cmd/claude-plus-desktop/frontend/src/Sessions.tsx`
- Modify: `wrapper/cmd/claude-plus-desktop/frontend/src/App.tsx`

- [ ] **Step 1: Create the Sessions panel**

Create `frontend/src/Sessions.tsx`:

```tsx
import { useEffect, useState } from "react";
import { EventsOn } from "../wailsjs/runtime/runtime";
import { ListSessions, Focus, NewSession } from "../wailsjs/go/main/App";
import { daemon } from "../wailsjs/go/models";

export default function Sessions() {
  const [sessions, setSessions] = useState<daemon.SessInfo[]>([]);

  useEffect(() => {
    // Seed from the attach ack, then keep updated via the sessions event.
    void ListSessions().then((s) => setSessions(s ?? []));
    const off = EventsOn("sessions:update", (list: daemon.SessInfo[]) => {
      setSessions(list ?? []);
    });
    return () => off();
  }, []);

  return (
    <aside style={{ width: 220, background: "#252526", color: "#ccc", padding: 8, overflowY: "auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <strong>Sessions</strong>
        <button onClick={() => void NewSession("")} title="New session">+</button>
      </div>
      {sessions.length === 0 ? (
        <p style={{ opacity: 0.6 }}>No sessions — click + to start one</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "8px 0" }}>
          {sessions.map((s) => (
            <li
              key={s.id}
              onClick={() => void Focus(s.id)}
              style={{
                padding: "4px 6px",
                cursor: "pointer",
                background: s.focused ? "#094771" : "transparent",
                borderRadius: 4,
              }}
            >
              {s.name || s.id} <small style={{ opacity: 0.6 }}>{s.status}</small>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
```

- [ ] **Step 2: Add the panel beside the terminal in App.tsx**

Overwrite `frontend/src/App.tsx`:

```tsx
import Terminal from "./Terminal";
import Sessions from "./Sessions";

function App() {
  return (
    <div style={{ display: "flex", height: "100vh", margin: 0 }}>
      <Sessions />
      <main style={{ flex: 1, minWidth: 0, background: "#1e1e1e" }}>
        <Terminal />
      </main>
    </div>
  );
}

export default App;
```

- [ ] **Step 3: Build and smoke-test session switching**

Run:
```bash
cd wrapper/cmd/claude-plus-desktop && wails dev
```
Expected: the session list shows the daemon's sessions; clicking the `+` spawns a new claude session (it appears in the list); clicking a row focuses it and the terminal pane follows. Close `wails dev`.

- [ ] **Step 4: Commit**

```bash
git add wrapper/cmd/claude-plus-desktop/frontend
git commit -m "feat(desktop): session-list panel (list, focus, new session)"
```

---

## Task 7: `claude+ --gui` discovery and launch

**Files:**
- Create: `wrapper/cmd/claude-plus/gui.go`
- Test: `wrapper/cmd/claude-plus/gui_test.go`
- Modify: `wrapper/cmd/claude-plus/main.go`

- [ ] **Step 1: Write the failing test for binary discovery**

Create `wrapper/cmd/claude-plus/gui_test.go`:

```go
package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestFindDesktopBinaryPrefersSibling(t *testing.T) {
	dir := t.TempDir()
	name := "claude-plus-desktop"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	sibling := filepath.Join(dir, name)
	if err := os.WriteFile(sibling, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	self := filepath.Join(dir, "claude-plus")
	got, err := findDesktopBinary(self)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if got != sibling {
		t.Fatalf("got %q, want %q", got, sibling)
	}
}

func TestFindDesktopBinaryMissingIsClearError(t *testing.T) {
	self := filepath.Join(t.TempDir(), "claude-plus")
	_, err := findDesktopBinary(self)
	if err == nil {
		t.Fatal("expected error when desktop binary absent")
	}
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd wrapper && go test ./cmd/claude-plus/ -run TestFindDesktopBinary -v`
Expected: FAIL to compile — `findDesktopBinary` undefined.

- [ ] **Step 3: Implement discovery + launch**

Create `wrapper/cmd/claude-plus/gui.go`:

```go
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// desktopBinName is the GUI binary's filename for this OS.
func desktopBinName() string {
	if runtime.GOOS == "windows" {
		return "claude-plus-desktop.exe"
	}
	return "claude-plus-desktop"
}

// findDesktopBinary locates the desktop GUI binary: first next to the CLI
// (selfPath's directory), then on PATH. Returns a clear error if absent.
func findDesktopBinary(selfPath string) (string, error) {
	name := desktopBinName()
	sibling := filepath.Join(filepath.Dir(selfPath), name)
	if fi, err := os.Stat(sibling); err == nil && !fi.IsDir() {
		return sibling, nil
	}
	if p, err := exec.LookPath(name); err == nil {
		return p, nil
	}
	return "", fmt.Errorf("desktop app not installed: %q not found next to the CLI or on PATH", name)
}

// runGUI discovers and launches the desktop binary, inheriting cwd so it targets
// the same per-repo daemon. It does not wait — the GUI owns its own lifecycle.
func runGUI() error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	bin, err := findDesktopBinary(self)
	if err != nil {
		return err
	}
	cmd := exec.Command(bin)
	cmd.Dir, _ = os.Getwd()
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return cmd.Start()
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd wrapper && go test ./cmd/claude-plus/ -run TestFindDesktopBinary -v`
Expected: both tests PASS.

- [ ] **Step 5: Add the `--gui` flag to main.go**

In `wrapper/cmd/claude-plus/main.go`, add a `gui` bool flag and dispatch it before the attach paths. Modify the flag block:

```go
	var sessionN int
	var showVersion bool
	var gui bool
	fs := flag.NewFlagSet("claude+", flag.ContinueOnError)
	fs.IntVar(&sessionN, "session", -1, "attach to the daemon at registry index N")
	fs.BoolVar(&showVersion, "version", false, "print version and exit")
	fs.BoolVar(&gui, "gui", false, "launch the desktop GUI for this repo")
	if err := fs.Parse(os.Args[1:]); err != nil {
		os.Exit(2)
	}

	if showVersion {
		fmt.Println("claude+", version)
		return
	}

	if gui {
		if err := runGUI(); err != nil {
			fail(err)
		}
		return
	}
```

- [ ] **Step 6: Verify the CLI builds and the flag is wired**

Run:
```bash
cd wrapper && go build ./cmd/claude-plus && go test ./cmd/claude-plus/ -count=1
```
Expected: build + tests pass. Manual check: `./claude-plus --gui` with no desktop binary present prints the clear "desktop app not installed" error.

- [ ] **Step 7: Commit**

```bash
git add wrapper/cmd/claude-plus/gui.go wrapper/cmd/claude-plus/gui_test.go wrapper/cmd/claude-plus/main.go
git commit -m "feat(cli): claude+ --gui discovers and launches the desktop binary"
```

---

## Task 8: Phase-1 verification and end-to-end smoke

**Files:** none (verification).

- [ ] **Step 1: Full module build + test**

Run:
```bash
cd wrapper && go build ./... && go test ./... -count=1
```
Expected: everything builds; `internal/desktop` and `cmd/claude-plus` tests pass; no regressions in `internal/daemon`/`internal/pty`.

- [ ] **Step 2: Build the desktop binary next to the CLI**

Run:
```bash
cd wrapper && go build -o ./build/claude-plus ./cmd/claude-plus
cd wrapper/cmd/claude-plus-desktop && wails build && cp build/bin/claude-plus-desktop* ../../build/
```
Expected: `wrapper/build/` contains both `claude-plus` and `claude-plus-desktop` side by side.

- [ ] **Step 3: End-to-end smoke via `--gui`**

From a git repo with the real `claude` on PATH:
```bash
cd <a-repo> && /path/to/wrapper/build/claude-plus --gui
```
Expected: a window opens, ensures/attaches the per-repo daemon, shows the session list, and renders a live claude session. `+` creates a session; clicking a row focuses it; typing drives claude; closing the window leaves the daemon running (confirm with `claude-plus ls`).

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore(desktop): phase-1 session-core end-to-end smoke verified"
```

---

## Verification checklist (Phase 1 done when all true)

- [ ] `go build ./...` and `go test ./...` pass in `wrapper/`.
- [ ] `internal/desktop` bridge unit tests cover output-emit, input-decode, list, and sessions-emit.
- [ ] `claude-plus-desktop` builds on this OS via `wails build` and opens a window.
- [ ] The window attaches the existing per-repo daemon, renders a live claude session, and forwards input + resize.
- [ ] The session-list panel lists sessions, `+` spawns one, clicking a row focuses it.
- [ ] `claude+ --gui` discovers and launches the desktop binary, with a clear error when it is absent.
- [ ] Closing the window leaves the daemon and sessions running (re-attachable from the terminal).

## Next phases (separate plans, written when Phase 1 lands)

- **Phase 2 — Concurrent per-client attach:** per-client focus + per-client PTY sizing in `pty.Mux`; protocol-version handshake in Hello/Ack; concurrent-attach tests. Unblocks terminal + desktop driving one daemon at once.
- **Phase 3 — Stream panel:** `FrameEvent` + a multi-consumer event tap in `daemon.Runtime`; run capture without HQ credentials; native event-feed panel.
- **Phase 4 — Status meters:** `FrameStatus` snapshot (tokens/cost/drift) + status-line UI.
- **Phase 5 — Agents/Skills:** `FrameAgentDiff` + reconcile control over `internal/config` sync; panel.
- **Phase 6 — Forge & Tickets:** desktop-side in-module HQ client (search + REST); "start session on ticket" via the existing `new` frame's `Ticket` field; panels.
- **Cross-cutting (late):** per-OS native Wails CI matrix; code signing/notarization.
