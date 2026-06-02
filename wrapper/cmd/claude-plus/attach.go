package main

import (
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/workflow-harness/claude-plus/internal/daemon"
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
func runShell(c *daemon.Client, instance string) error {
	inFd := int(os.Stdin.Fd())
	outFd := int(os.Stdout.Fd())

	w, h, err := term.GetSize(outFd)
	if err != nil || w <= 0 || h <= 0 {
		w, h = 80, 24
	}

	// Alternate screen + raw mode + SGR mouse reporting; restore all on exit.
	// 1000 = click tracking, 1006 = SGR extended coordinates (cols/rows > 223).
	os.Stdout.WriteString("\x1b[?1049h\x1b[2J\x1b[?1000h\x1b[?1006h")
	defer os.Stdout.WriteString("\x1b[?1000l\x1b[?1006l\x1b[?25h\x1b[?1049l")
	if term.IsTerminal(inFd) {
		if old, mkErr := term.MakeRaw(inFd); mkErr == nil {
			defer func() { _ = term.Restore(inFd, old) }()
		}
	}

	screen := shell.NewScreen(w, h, os.Stdout)
	comp := shell.NewCompositor(screen, instance)

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

	c.Out = func(b []byte) {
		comp.FeedOutput(comp.FocusedSessionID(), b)
		markDirty()
	}
	c.OnSessions = func(list []daemon.SessInfo) {
		applySessions(comp, list)
		markDirty()
	}

	readErr := make(chan error, 1)
	go func() { readErr <- c.Run() }()

	events := make(chan inputEvent, 1024)
	go parseInput(os.Stdin, events)

	frame := time.NewTicker(16 * time.Millisecond)
	defer frame.Stop()
	resize := time.NewTicker(250 * time.Millisecond)
	defer resize.Stop()
	lastW, lastH := w, h

	markDirty() // initial paint
	prefix := false

	for {
		select {
		case <-readErr:
			return nil // daemon dropped the connection (or we detached)
		case <-frame.C:
			select {
			case <-dirty:
				comp.Render()
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
			if ev.mouse {
				if ev.press && ev.button == 0 { // plain left-click
					changed, sess, newSess := comp.Click(ev.x, ev.y)
					switch {
					case newSess:
						_ = c.NewSession("")
						markDirty()
					case sess != "":
						_ = c.Focus(sess)
						markDirty()
					case changed:
						markDirty()
					}
				}
				continue
			}
			// Multi-byte escape sequences (arrows, etc.) forward intact to claude.
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
				case actNewSession:
					_ = c.NewSession("")
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
)

const ctrlG = 0x07

func handleKey(c *daemon.Client, comp *shell.Compositor, b byte, prefix *bool) keyAction {
	if *prefix {
		*prefix = false
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
		}
		return actHandled
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
