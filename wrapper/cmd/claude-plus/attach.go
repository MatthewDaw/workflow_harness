package main

import (
	"fmt"
	"io"
	"os"
	"os/signal"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/workflow-harness/claude-plus/internal/daemon"
	"github.com/workflow-harness/claude-plus/internal/diag"
	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/shell"
	"golang.org/x/term"
)

// runShell drives the full claude+ chrome: it frames the live hosted session
// inside the tab bar, session sub-tabs, and status line, mirroring claude's PTY
// output through a virtual terminal so it renders correctly (and over SSH). The
// daemon owns the real session; this is a thin rendering + input client.
//
// Keys use a tmux-style prefix (Ctrl-G) so claude keeps every other key:
//
//	Ctrl-G n / Tab   next tab        Ctrl-G p   prev tab
//	Ctrl-G 1..5      jump to a tab    Ctrl-G d   detach (daemon + session live on)
//	Ctrl-G c         new session      Ctrl-G q   quit (end sessions + stop daemon)
func runShell(c *daemon.Client, instance, hqProject string) error {
	inFd := int(os.Stdin.Fd())
	outFd := int(os.Stdout.Fd())

	w, h, err := term.GetSize(outFd)
	if err != nil || w <= 0 || h <= 0 {
		w, h = 80, 24
	}

	// Alternate screen + raw mode + SGR mouse reporting; restore all on exit.
	// 1000 = click tracking, 1006 = SGR extended coordinates (cols/rows > 223).
	//
	// Teardown MUST happen on every exit path — including a panic in a background
	// goroutine or an external terminate signal — or the user's terminal is left
	// stuck in raw + mouse-reporting mode (keystrokes/mouse echo as escape
	// sequences). restore() is idempotent and undoes everything: mouse off, alt
	// screen off, cursor shown, raw mode restored.
	var rawState *term.State
	if term.IsTerminal(inFd) {
		if old, mkErr := term.MakeRaw(inFd); mkErr == nil {
			rawState = old
		}
	}
	os.Stdout.WriteString("\x1b[?1049h\x1b[2J\x1b[?1000h\x1b[?1006h")
	var restoreOnce sync.Once
	restore := func() {
		restoreOnce.Do(func() {
			os.Stdout.WriteString("\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[?1049l")
			if rawState != nil {
				_ = term.Restore(inFd, rawState)
			}
		})
	}
	defer restore()

	// External terminate signals (window close, SIGTERM, SIGHUP) bypass deferred
	// teardown — catch them and restore before exiting. Ctrl-C is delivered as a
	// raw byte (ISIG is off in raw mode), so it still reaches claude.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigCh
		restore()
		os.Exit(1)
	}()

	// A panic in either background goroutine would otherwise kill the process
	// without running the deferred restore. Capture it, restore, and exit clean.
	guard := func(where string) {
		if r := recover(); r != nil {
			diag.LogPanic(where, r, debug.Stack())
			restore()
			os.Exit(1)
		}
	}

	screen := shell.NewScreen(w, h, os.Stdout)
	comp := shell.NewCompositor(screen, instance)

	// Label the top bar with the signed-in identity (decoded from the local
	// device token), or "not signed in" when there are no credentials.
	if id, ok := loadIdentity(); ok {
		comp.SetIdentity(id.Name, id.Org, true)
	} else {
		comp.SetIdentity("", "", false)
	}
	// Name the CommandHQ project this repo is linked to next to the HQ indicator.
	comp.SetHQProject(hqProject)

	// Seed the sub-tab row from the attach ack, and size the hosted PTY to the
	// body region so claude renders at the framed size.
	applySessions(comp, c.Sessions)
	iw, ih := comp.InnerSize()
	_ = c.Resize(iw, ih)

	dirty := make(chan struct{}, 1)
	markDirty := func() {
		select {
		case dirty <- struct{}{}:
		default:
		}
	}

	wireClientHandlers(c, comp, markDirty)

	readErr := make(chan error, 1)
	go func() {
		defer guard("client.Run")
		readErr <- c.Run()
	}()

	events := make(chan inputEvent, 1024)
	go func() {
		defer guard("parseInput")
		parseInput(os.Stdin, events)
	}()

	frame := time.NewTicker(16 * time.Millisecond)
	defer frame.Stop()
	resize := time.NewTicker(250 * time.Millisecond)
	defer resize.Stop()
	lastW, lastH := w, h

	markDirty() // initial paint
	prefix := false

	// Double-click detection is delegated to a clickTracker because
	// Compositor.Click is a pure hit-test with no timing: a second left-click on
	// the SAME session sub-tab within the window opens the inline rename draft
	// (the terminal analogue of the desktop double-click <input>); otherwise the
	// hit just focuses it.
	var tracker clickTracker

	for {
		select {
		case <-readErr:
			return nil // daemon dropped the connection (or we detached)
		case <-frame.C:
			select {
			case <-dirty:
				// Swallow a transient render panic (e.g. an emulator edge case)
				// so one bad frame is logged, not fatal — the chrome keeps running.
				func() {
					defer diag.Recover("render")
					comp.Render()
				}()
			default:
			}
		case <-resize.C:
			nw, nh, e := term.GetSize(outFd)
			if e == nil && nw > 0 && nh > 0 && (nw != lastW || nh != lastH) {
				lastW, lastH = nw, nh
				screen.Resize(nw, nh)
				comp.ResizePanes()
				riw, rih := comp.InnerSize()
				_ = c.Resize(riw, rih)
				markDirty()
			}
		case ev, ok := <-events:
			if !ok {
				events = nil // stdin closed; keep rendering until detach or daemon drop
				continue
			}
			// The Ctrl-G prefix is authoritative and tab-independent: once it is
			// armed, the very NEXT input event resolves the chord no matter what
			// shape that event takes. This MUST run before the mouse and
			// escape-sequence branches below — otherwise, on the Session tab where
			// claude streams a constant flow of mouse/escape traffic, a multi-byte
			// event (e.g. the terminal coalescing the chord key with an arrow, or
			// an SGR mouse report) would short-circuit to `continue` and silently
			// strand the pending prefix, so Ctrl-G d never reaches actDetach. That
			// short-circuit is exactly why detach worked on non-Session tabs (no
			// competing escape/mouse traffic) but not on the Session tab.
			if prefix {
				prefix = false
				// A mouse event (or empty event) after Ctrl-G is not a valid chord;
				// cancel the prefix cleanly without acting.
				if ev.mouse || len(ev.bytes) == 0 {
					continue
				}
				switch resolvePrefixed(comp, ev.bytes[0]) {
				case actDetach:
					_ = c.Detach()
					return nil
				case actQuit:
					_ = c.Shutdown()
					return nil
				case actNewSession:
					_ = c.NewSession()
					markDirty()
				default:
					markDirty()
				}
				continue
			}
			if ev.mouse {
				// handleMouse applies the chrome-local effects and reports the
				// daemon-facing ones; we just dispatch those. A commit can ride
				// along with another action (a click elsewhere blurs the draft,
				// then does its own thing), so the fields are not exclusive.
				eff := tracker.handleMouse(comp, ev, time.Now())
				if eff.renameID != "" {
					_ = c.Rename(eff.renameID, eff.renameName)
				}
				if eff.sendInput != nil {
					_ = c.Input(eff.sendInput)
				}
				if eff.focusID != "" {
					_ = c.Focus(eff.focusID)
				}
				if eff.closeID != "" {
					_ = c.CloseSession(eff.closeID)
				}
				if eff.newSession {
					_ = c.NewSession()
				}
				if eff.dirty {
					markDirty()
				}
				continue
			}
			// While an inline rename draft is open the keyboard drives the draft,
			// not the hosted session: intercept every keystroke here BEFORE the
			// escape-sequence forward and the handleKey/forward path below.
			if comp.Editing() {
				if len(ev.bytes) == 0 {
					continue
				}
				b := ev.bytes[0]
				switch {
				case b == 0x0d || b == 0x0a: // Enter -> commit
					if id, name, ok := comp.CommitRename(); ok {
						_ = c.Rename(id, name)
					}
					markDirty()
				case b == 0x1b || b == 0x03: // ESC or Ctrl-C -> cancel
					// Ctrl-C is a single byte, so it cancels immediately. A lone ESC
					// is buffered by parseInput's state machine and only arrives once
					// coalesced with the next key — Ctrl-C is the reliable cancel. A
					// leading ESC (arrow-key sequence) also cancels rather than
					// corrupting the draft.
					comp.CancelRename()
					markDirty()
				default:
					// Backspace (0x7f/0x08) and printable bytes feed the draft.
					for _, rb := range ev.bytes {
						comp.RenameInput(rb)
					}
					markDirty()
				}
				continue
			}
			// Multi-byte escape sequences (arrows, etc.) forward intact to claude.
			// (A pending prefix was already resolved above, so a leading 0x07 here
			// can only be a literal Ctrl-G inside a longer sequence — never a
			// stranded prefix.)
			if len(ev.bytes) > 1 && ev.bytes[0] == 0x1b {
				if comp.ActiveIsSession() {
					_ = c.Input(ev.bytes)
				}
				continue
			}
			for _, b := range ev.bytes {
				switch handleKey(c, comp, b, &prefix) {
				case actDetach:
					_ = c.Detach()
					return nil
				case actQuit:
					_ = c.Shutdown()
					return nil
				case actNewSession:
					_ = c.NewSession()
					markDirty()
				case actHandled:
					markDirty()
				case actForward:
					if comp.ActiveIsSession() {
						_ = c.Input([]byte{b})
					}
				}
			}
		}
	}
}

// wireClientHandlers subscribes the compositor to every daemon push channel:
// PTY output (the Session tab), session-list updates (the sub-tab row), captured
// events (the Stream tab), and meter snapshots (the status line). Each handler
// marks the frame dirty so the next tick repaints. Extracted from runShell so the
// subscriptions are unit-testable without a live daemon or terminal — previously
// OnEvent/OnStatus were never set, which is why the Stream tab and meter stayed
// blank regardless of activity.
func wireClientHandlers(c *daemon.Client, comp *shell.Compositor, markDirty func()) {
	c.Out = func(sessID string, b []byte) {
		comp.FeedOutput(sessID, b)
		markDirty()
	}
	c.OnSessions = func(list []daemon.SessInfo) {
		applySessions(comp, list)
		markDirty()
	}
	c.OnEvent = func(env event.Envelope) {
		comp.FeedEvent(env)
		markDirty()
	}
	c.OnStatus = func(s daemon.StatusSnapshot) {
		comp.SetStatus(int(s.Tokens))
		markDirty()
	}
}

func applySessions(comp *shell.Compositor, list []daemon.SessInfo) {
	subs := make([]shell.SubTab, len(list))
	focused := -1
	for i, si := range list {
		subs[i] = shell.SubTab{ID: si.ID, Name: si.Name, Status: si.Status}
		if si.Focused {
			focused = i
		}
	}
	if focused == -1 && len(subs) > 0 {
		focused = 0
	}
	comp.SetSubs(subs, focused)
}

type keyAction int

const (
	actForward    keyAction = iota // pass the byte to the hosted session
	actHandled                     // consumed by the chrome
	actDetach                      // leave the client; daemon survives
	actNewSession                  // spawn another claude session in this instance
	actQuit                        // end every session (HQ sees done) + stop the daemon
)

const ctrlG = 0x07

// resolvePrefixed maps the byte following a Ctrl-G prefix to a chrome action and
// applies any tab navigation as a side effect on comp. It is intentionally free
// of *daemon.Client so the chord semantics (especially Ctrl-G d -> actDetach)
// can be unit-tested without a live daemon or terminal. Callers translate the
// returned actDetach/actNewSession into the corresponding client calls.
//
// Detach is bound to BOTH `d` and a repeated Ctrl-G so it stays reachable even
// if a terminal swallows or rewrites a literal `d`; we deliberately do not bind
// any other always-on key, so claude keeps every keystroke it needs.
func resolvePrefixed(comp *shell.Compositor, b byte) keyAction {
	switch b {
	case 'n', 0x09: // n or Tab
		comp.NextTab()
	case 'p':
		comp.PrevTab()
	case '1', '2', '3', '4', '5':
		comp.SetTab(int(b - '1'))
	case 'c':
		return actNewSession
	case 'd', ctrlG:
		return actDetach
	case 'q':
		return actQuit
	}
	return actHandled
}

func handleKey(c *daemon.Client, comp *shell.Compositor, b byte, prefix *bool) keyAction {
	if *prefix {
		*prefix = false
		return resolvePrefixed(comp, b)
	}
	if b == ctrlG {
		*prefix = true
		return actHandled
	}
	// On non-session tabs there is no hosted session to type into; let plain
	// keys navigate the chrome directly.
	if !comp.ActiveIsSession() {
		switch b {
		case 0x09:
			comp.NextTab()
			return actHandled
		case '1', '2', '3', '4', '5':
			comp.SetTab(int(b - '1'))
			return actHandled
		case 'q', 0x1b:
			comp.SetTab(0)
			return actHandled
		}
	}
	return actForward
}

// dblClickWindow is the maximum gap between two left-clicks on the same session
// sub-tab for the second to count as a double-click (which opens the inline
// rename draft rather than re-focusing the tab).
const dblClickWindow = 400 * time.Millisecond

// clickTracker carries the cross-event state that double-click detection needs —
// the last sub-tab clicked and when — which Compositor.Click, a pure timing-free
// hit-test, deliberately does not hold.
type clickTracker struct {
	lastSess string
	lastAt   time.Time
}

// mouseEffect is the daemon-facing result of handling one mouse event. The
// chrome-local effects (active-tab switch, focus highlight, rename draft, scroll)
// are already applied inside handleMouse; the caller only dispatches the set
// fields to the daemon and repaints when dirty. Fields are not mutually
// exclusive: a click that blurs an open draft both commits (renameID) and then
// performs its own action (e.g. focusID).
type mouseEffect struct {
	renameID, renameName string // commit an open rename draft: id + trimmed name
	sendInput            []byte // forward an SGR mouse sequence to the focused session
	focusID              string // focus this session sub-tab
	closeID              string // close this session
	newSession           bool   // spawn a new session
	dirty                bool   // a repaint is needed
}

// handleMouse resolves one mouse event against the chrome. `now` is injected so
// the double-click timing is unit-testable.
//
// The commit-open-draft step is gated on ev.press for a specific reason: a
// double-click arrives as press, release, press, release. The second press opens
// the rename draft; the button RELEASE that immediately follows is part of that
// same gesture, not a click elsewhere — so it must NOT commit the freshly opened
// (still-unchanged) draft. The original code committed on ANY mouse event, so
// that trailing release closed the draft instantly and the user could never type
// into it. Only a later press (a genuine click elsewhere) blurs and commits.
func (ct *clickTracker) handleMouse(comp *shell.Compositor, ev inputEvent, now time.Time) mouseEffect {
	var eff mouseEffect
	if ev.press && comp.Editing() {
		if id, name, ok := comp.CommitRename(); ok {
			eff.renameID, eff.renameName = id, name
		}
		eff.dirty = true
	}
	// When the focused session has enabled mouse tracking (claude does, in the
	// alternate screen), a body event belongs to claude — forward it verbatim so
	// wheel-scroll and selection behave exactly as running claude directly. Chrome
	// rows (tabs, sub-tabs, status) fall outside the body and stay with the chrome.
	if comp.ActiveIsSession() {
		if col, row, inBody := comp.BodyMouse(ev.x, ev.y); inBody && comp.FocusedMouseTracking() {
			eff.sendInput = encodeSGRMouse(ev.button, col, row, ev.press)
			return eff
		}
	}
	if ev.press && ev.button == 0 { // plain left-click
		switch res := comp.Click(ev.x, ev.y); {
		case res.NewSession:
			eff.newSession, eff.dirty = true, true
		case res.CloseSessID != "":
			eff.closeID, eff.dirty = res.CloseSessID, true
		case res.FocusSessID != "":
			// Second click on the same sub-tab inside the window = double-click ->
			// open the inline rename draft instead of re-focusing.
			if res.FocusSessID == ct.lastSess && now.Sub(ct.lastAt) < dblClickWindow {
				comp.BeginRename(res.FocusSessID)
				ct.lastSess = "" // consume; a 3rd click shouldn't re-trigger
			} else {
				eff.focusID = res.FocusSessID
				ct.lastSess, ct.lastAt = res.FocusSessID, now
			}
			eff.dirty = true
		case res.Changed:
			eff.dirty = true
		}
	} else if ev.press && comp.ActiveIsSession() {
		// Wheel over the chrome scrolls the focused session's scrollback.
		// SGR codes: 64 = wheel up (toward history), 65 = wheel down.
		switch ev.button {
		case 64:
			comp.ScrollUp(3)
			eff.dirty = true
		case 65:
			comp.ScrollDown(3)
			eff.dirty = true
		}
	}
	return eff
}

// inputEvent is a decoded stdin event: either raw key bytes (to interpret as
// chrome keybinds or forward to the session) or a mouse event.
type inputEvent struct {
	mouse  bool
	bytes  []byte // non-mouse: raw bytes
	x, y   int    // mouse: 0-based screen coords
	button int    // mouse: raw SGR button code (0 = plain left)
	press  bool   // mouse: press (M) vs release (m)
}

// parseInput reads stdin and emits key/mouse events. SGR mouse sequences
// (ESC [ < b ; col ; row M|m) are decoded so clicks reach the chrome; every
// other byte/sequence passes through as raw bytes so the keyboard still drives
// claude.
func parseInput(r io.Reader, out chan<- inputEvent) {
	buf := make([]byte, 1024)
	var esc, mb []byte
	state := 0 // 0 normal, 1 ESC, 2 CSI, 3 mouse params
	emit := func(bs ...byte) { out <- inputEvent{bytes: append([]byte(nil), bs...)} }
	for {
		n, e := r.Read(buf)
		for i := 0; i < n; i++ {
			b := buf[i]
			switch state {
			case 0:
				if b == 0x1b {
					esc = []byte{b}
					state = 1
				} else {
					emit(b)
				}
			case 1: // after ESC
				esc = append(esc, b)
				if b == '[' {
					state = 2
				} else {
					emit(esc...)
					esc, state = nil, 0
				}
			case 2: // after ESC [
				if b == '<' {
					state, mb = 3, mb[:0]
				} else {
					esc = append(esc, b)
					emit(esc...)
					esc, state = nil, 0
				}
			case 3: // mouse params after ESC [ <
				if b == 'M' || b == 'm' {
					if ev, ok := parseSGRMouse(mb, b == 'M'); ok {
						out <- ev
					}
					esc, state = nil, 0
				} else {
					mb = append(mb, b)
				}
			}
		}
		if e != nil {
			close(out)
			return
		}
	}
}

// encodeSGRMouse re-serializes a decoded mouse event as the SGR sequence
// (ESC [ < button ; col ; row M|m) that the hosted session's PTY expects, the
// inverse of parseSGRMouse. col/row are 1-based; press selects the M (press) vs
// m (release) terminator. Wheel events (button 64/65) are press-only.
func encodeSGRMouse(button, col, row int, press bool) []byte {
	term := byte('m')
	if press {
		term = 'M'
	}
	return []byte(fmt.Sprintf("\x1b[<%d;%d;%d%c", button, col, row, term))
}

func parseSGRMouse(params []byte, press bool) (inputEvent, bool) {
	parts := strings.Split(string(params), ";") // "button;col;row"
	if len(parts) != 3 {
		return inputEvent{}, false
	}
	btn, e1 := strconv.Atoi(parts[0])
	col, e2 := strconv.Atoi(parts[1])
	row, e3 := strconv.Atoi(parts[2])
	if e1 != nil || e2 != nil || e3 != nil {
		return inputEvent{}, false
	}
	return inputEvent{mouse: true, button: btn, x: col - 1, y: row - 1, press: press}, true
}
