// Package shell implements the claude+ terminal chrome: a hand-rolled screen
// compositor (like tmux) that frames a live claude session inside the tab bar,
// session sub-tabs, and status line from the wireframe.
//
// The Session pane is a real virtual terminal (vt10x): claude's PTY output is
// fed in, and the compositor reads the resulting cell grid to render it inside
// the chrome. This is what makes the embedded session render correctly (cursor
// motion, screen clears, colors) rather than as a scrollback blob — and it
// works over SSH because it emits nothing but ANSI to stdout.
package shell

import (
	"sync"

	vt "github.com/hinshun/vt10x"
)

// Pane is the virtual terminal for one session.
type Pane struct {
	mu         sync.Mutex
	term       vt.Terminal
	cols, rows int
}

// NewPane creates a virtual terminal of the given size.
func NewPane(cols, rows int) *Pane {
	cols, rows = clampSize(cols, rows)
	return &Pane{term: vt.New(vt.WithSize(cols, rows)), cols: cols, rows: rows}
}

// Write feeds raw claude PTY output into the emulator.
func (p *Pane) Write(b []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	_, _ = p.term.Write(b)
}

// Resize changes the virtual terminal dimensions.
func (p *Pane) Resize(cols, rows int) {
	cols, rows = clampSize(cols, rows)
	p.mu.Lock()
	defer p.mu.Unlock()
	p.term.Resize(cols, rows)
	p.cols, p.rows = cols, rows
}

// Size returns the current dimensions.
func (p *Pane) Size() (cols, rows int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.cols, p.rows
}

// Cell returns the glyph at (x, y) in the emulator grid. Out-of-range coordinates
// return a blank glyph rather than panicking — the emulator's grid and a caller's
// cached dimensions can momentarily disagree across a resize.
func (p *Pane) Cell(x, y int) vt.Glyph {
	p.mu.Lock()
	defer p.mu.Unlock()
	if x < 0 || y < 0 || x >= p.cols || y >= p.rows {
		return vt.Glyph{Char: ' ', FG: vt.DefaultFG, BG: vt.DefaultBG}
	}
	return p.term.Cell(x, y)
}

// History returns the session's scrollback — lines evicted off the top of the
// live grid, oldest-first. Backed by the patched vt10x history ring; the
// returned glyph slices are copies safe to read after the lock is released.
func (p *Pane) History() [][]vt.Glyph {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.term.Lock()
	h := p.term.Scrollback()
	p.term.Unlock()
	return h
}

// MouseTracking reports whether the session has enabled any xterm mouse-tracking
// mode (DECSET 1000/1002/1003 etc.). claude turns this on for its own scroll and
// selection, which means wheel/click events over the body belong to claude — the
// chrome must forward them rather than consume them for its own (alt-screen-empty)
// scrollback.
func (p *Pane) MouseTracking() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.term.Mode()&vt.ModeMouseMask != 0
}

// Cursor returns the emulator cursor position and visibility, clamped into the
// current grid so callers can index Cell with it safely.
func (p *Pane) Cursor() (x, y int, visible bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	c := p.term.Cursor()
	cx, cy := int(c.X), int(c.Y)
	if cx < 0 {
		cx = 0
	} else if cx >= p.cols {
		cx = p.cols - 1
	}
	if cy < 0 {
		cy = 0
	} else if cy >= p.rows {
		cy = p.rows - 1
	}
	return cx, cy, p.term.CursorVisible()
}

func clampSize(cols, rows int) (int, int) {
	if cols < 1 {
		cols = 1
	}
	if rows < 1 {
		rows = 1
	}
	return cols, rows
}
