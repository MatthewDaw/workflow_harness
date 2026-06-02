package shell

import (
	"fmt"
	"sync"

	vt "github.com/hinshun/vt10x"
)

// Chrome layout: row 0 is the tab bar, row 1 the session sub-tabs, the last row
// the status line, and everything between is the body (the live session pane or
// a non-session tab view).
const (
	rowTabBar  = 0
	rowSubTabs = 1
	bodyTop    = 2
	chromeRows = 3 // tab bar + sub-tabs + status line
)

var tabNames = []string{"Session", "Tickets", "Agents", "Forge", "Stream"}

// Palette (256-color indices).
const (
	colDim    vt.Color = 244
	colAccent vt.Color = 75
	colGreen  vt.Color = 71
	colAmber  vt.Color = 179
	colBarBG  vt.Color = 236
	colText   vt.Color = 252
)

// SubTab is one entry in the session sub-tab row.
type SubTab struct {
	ID     string
	Name   string
	Status string // active | needs_input | idle | done
}

// Compositor frames a live session inside the claude+ chrome.
type Compositor struct {
	mu sync.Mutex

	screen   *Screen
	instance string

	active     int // index into tabNames
	subs       []SubTab
	focusedSub int
	panes      map[string]*Pane // sessionID -> mirror terminal

	tokens   int
	costUSD  float64
	degraded bool
}

// NewCompositor builds a compositor rendering to screen.
func NewCompositor(screen *Screen, instance string) *Compositor {
	return &Compositor{
		screen:     screen,
		instance:   instance,
		panes:      map[string]*Pane{},
		focusedSub: -1,
	}
}

// InnerSize returns the body region size — what the hosted PTY should be sized to.
func (c *Compositor) InnerSize() (w, h int) {
	w, sh := c.screen.Size()
	h = sh - chromeRows
	if h < 1 {
		h = 1
	}
	return w, h
}

// EnsurePane returns the mirror terminal for a session, creating it at the
// current inner size.
func (c *Compositor) EnsurePane(sessID string) *Pane {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ensurePaneLocked(sessID)
}

func (c *Compositor) ensurePaneLocked(sessID string) *Pane {
	p, ok := c.panes[sessID]
	if !ok {
		w, h := c.InnerSize()
		p = NewPane(w, h)
		c.panes[sessID] = p
	}
	return p
}

// FeedOutput writes hosted-session output into that session's mirror terminal.
func (c *Compositor) FeedOutput(sessID string, data []byte) {
	c.mu.Lock()
	p := c.ensurePaneLocked(sessID)
	c.mu.Unlock()
	p.Write(data)
}

// ResizePanes resizes every mirror terminal to the current inner size.
func (c *Compositor) ResizePanes() {
	c.mu.Lock()
	defer c.mu.Unlock()
	w, h := c.InnerSize()
	for _, p := range c.panes {
		p.Resize(w, h)
	}
}

// SetSubs updates the session sub-tab row and which one is focused.
func (c *Compositor) SetSubs(subs []SubTab, focused int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.subs = subs
	c.focusedSub = focused
}

// SetStatus updates the token/cost readout.
func (c *Compositor) SetStatus(tokens int, costUSD float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.tokens, c.costUSD = tokens, costUSD
}

// SetDegraded flags HQ-unreachable state for the chrome.
func (c *Compositor) SetDegraded(down bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.degraded = down
}

// Tab navigation.
func (c *Compositor) NextTab() { c.mu.Lock(); c.active = (c.active + 1) % len(tabNames); c.mu.Unlock() }
func (c *Compositor) PrevTab() {
	c.mu.Lock()
	c.active = (c.active - 1 + len(tabNames)) % len(tabNames)
	c.mu.Unlock()
}
func (c *Compositor) SetTab(i int) {
	c.mu.Lock()
	if i >= 0 && i < len(tabNames) {
		c.active = i
	}
	c.mu.Unlock()
}

// ActiveIsSession reports whether the Session tab is active (keystrokes route to
// the hosted session only then).
func (c *Compositor) ActiveIsSession() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.active == 0
}

// FocusedSessionID returns the currently focused session's id, if any.
func (c *Compositor) FocusedSessionID() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.focusedSub >= 0 && c.focusedSub < len(c.subs) {
		return c.subs[c.focusedSub].ID
	}
	return ""
}

// Render composes a frame and paints it.
func (c *Compositor) Render() {
	c.mu.Lock()
	defer c.mu.Unlock()

	s := c.screen
	w, h := s.Size()
	s.ClearBack()

	c.renderTabBar(w)
	c.renderSubTabs(w)

	curX, curY, curVis := 0, 0, false
	if c.active == 0 {
		curX, curY, curVis = c.renderSessionBody(w, h)
	} else {
		c.renderPlaceholderBody(w, h)
	}

	c.renderStatusLine(w, h)
	s.Flush(curX, curY, curVis)
}

func (c *Compositor) renderTabBar(w int) {
	x := 0
	for i, name := range tabNames {
		label := " " + name + " "
		active := i == c.active
		fg := colDim
		if active {
			fg = colText
		}
		c.screen.SetString(x, rowTabBar, label, fg, vt.DefaultBG, active, active)
		x += len(label) + 1
	}
	// HQ-linked indicator on the right.
	ind := "● HQ linked"
	indFG := colGreen
	if c.degraded {
		ind = "● HQ offline"
		indFG = colAmber
	}
	c.screen.SetString(w-len(ind)-1, rowTabBar, ind, indFG, vt.DefaultBG, false, false)
}

func (c *Compositor) renderSubTabs(w int) {
	if len(c.subs) == 0 {
		c.screen.SetString(0, rowSubTabs, " (no sessions) ", colDim, vt.DefaultBG, false, false)
		return
	}
	x := 0
	for i, st := range c.subs {
		dot := "●"
		dotFG := colGreen
		switch st.Status {
		case "needs_input":
			dotFG = colAmber
		case "idle", "done":
			dotFG = colDim
		}
		focused := i == c.focusedSub
		c.screen.SetString(x, rowSubTabs, " ", vt.DefaultFG, vt.DefaultBG, false, false)
		x++
		c.screen.SetString(x, rowSubTabs, dot, dotFG, vt.DefaultBG, false, false)
		x += 2
		nameFG := colDim
		if focused {
			nameFG = colText
		}
		c.screen.SetString(x, rowSubTabs, st.Name+" ", nameFG, vt.DefaultBG, focused, false)
		x += len(st.Name) + 2
		if x >= w {
			break
		}
	}
}

// renderSessionBody blits the focused session's mirror grid into the body and
// returns the hardware cursor position (offset into the body region).
func (c *Compositor) renderSessionBody(w, h int) (curX, curY int, curVis bool) {
	bodyH := h - chromeRows
	if bodyH < 1 {
		return 0, 0, false
	}
	id := ""
	if c.focusedSub >= 0 && c.focusedSub < len(c.subs) {
		id = c.subs[c.focusedSub].ID
	}
	p := c.panes[id]
	if p == nil {
		// Fall back to any single pane (common single-session case).
		for _, only := range c.panes {
			p = only
			break
		}
	}
	if p == nil {
		c.screen.SetString(2, bodyTop, "(starting session…)", colDim, vt.DefaultBG, false, false)
		return 0, 0, false
	}
	pw, ph := p.Size()
	for y := 0; y < ph && y < bodyH; y++ {
		for x := 0; x < pw && x < w; x++ {
			g := p.Cell(x, y)
			ch := g.Char
			if ch == 0 {
				ch = ' '
			}
			c.screen.Set(x, bodyTop+y, Cell{Ch: ch, FG: g.FG, BG: g.BG})
		}
	}
	cx, cy, vis := p.Cursor()
	return cx, bodyTop + cy, vis
}

func (c *Compositor) renderPlaceholderBody(w, h int) {
	name := tabNames[c.active]
	msg := fmt.Sprintf("%s — coming from HQ (this tab's live data wiring is next)", name)
	c.screen.SetString(2, bodyTop, msg, colText, vt.DefaultBG, true, false)
	c.screen.SetString(2, bodyTop+2, "Press ⇥ or ⌃T to return to the Session.", colDim, vt.DefaultBG, false, false)
}

func (c *Compositor) renderStatusLine(w, h int) {
	row := h - 1
	if row < 0 {
		return
	}
	// Full-width reverse bar.
	for x := 0; x < w; x++ {
		c.screen.Set(x, row, Cell{Ch: ' ', FG: colText, BG: colBarBG})
	}
	focused := "—"
	if c.focusedSub >= 0 && c.focusedSub < len(c.subs) {
		focused = c.subs[c.focusedSub].Name
	}
	left := fmt.Sprintf(" %s  ▸ %s  %dtok  $%.2f", c.instance, focused, c.tokens, c.costUSD)
	c.screen.SetString(0, row, left, colText, colBarBG, false, false)
	hint := "⇥ tab · ⌃1-9 session · ⌃G detach "
	c.screen.SetString(w-len(hint), row, hint, colDim, colBarBG, false, false)
}
