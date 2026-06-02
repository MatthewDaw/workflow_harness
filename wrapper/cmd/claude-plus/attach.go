package main

import (
	"os"
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

	// Alternate screen + raw mode; restore both on exit.
	os.Stdout.WriteString("\x1b[?1049h\x1b[2J")
	defer os.Stdout.WriteString("\x1b[?25h\x1b[?1049l")
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

	keyCh := make(chan byte, 1024)
	go func() {
		buf := make([]byte, 1024)
		for {
			n, e := os.Stdin.Read(buf)
			for i := 0; i < n; i++ {
				keyCh <- buf[i]
			}
			if e != nil {
				close(keyCh)
				return
			}
		}
	}()

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
		case b, ok := <-keyCh:
			if !ok {
				keyCh = nil // stdin closed; keep rendering until detach or daemon drop
				continue
			}
			switch handleKey(c, comp, b, &prefix) {
			case actDetach:
				_ = c.Detach()
				return nil
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
	actForward keyAction = iota // pass the byte to the hosted session
	actHandled                  // consumed by the chrome
	actDetach                   // leave the client; daemon survives
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
