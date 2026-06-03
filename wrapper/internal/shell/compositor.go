package shell

import (
	"fmt"
	"sync"

	vt "github.com/hinshun/vt10x"
	"github.com/mattn/go-runewidth"

	"github.com/workflow-harness/claude-plus/internal/event"
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

// Fuzzy read-side "Forge" is hidden in the live shell per the current model
// (Forge is driven from the /startforge–/endforge PTY flow, not a tab).
var tabNames = []string{"Session", "Agents", "Stream"}

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

// span is a clickable horizontal range [lo,hi) on a chrome row, mapped to an
// index (tab index or sub-tab index) for mouse hit-testing.
type span struct{ lo, hi, idx int }

// Compositor frames a live session inside the claude+ chrome.
type Compositor struct {
	mu sync.Mutex

	screen   *Screen
	instance string

	active     int // index into tabNames
	subs       []SubTab
	focusedSub int
	panes      map[string]*Pane // sessionID -> mirror terminal

	// Click regions recomputed each render so mouse hit-testing matches exactly
	// what was drawn.
	tabSpans []span
	subSpans []span
	newSpan  span // the "+ new" session affordance in the sub-tab row

	tokens   int
	costUSD  float64
	degraded bool

	// streamEvents is the bounded live event feed rendered on the Stream tab
	// (oldest first, newest at the bottom). Fed from the daemon's event bus via
	// FeedEvent — the terminal counterpart to the desktop Stream panel.
	streamEvents []event.Envelope
}

// maxStreamEvents bounds the Stream tab's in-memory feed, mirroring the desktop
// panel's cap so a long-lived session can't grow it without bound.
const maxStreamEvents = 300

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

// FeedEvent appends a captured event envelope to the Stream tab's bounded feed.
// Safe to call from the attach read loop concurrently with Render.
func (c *Compositor) FeedEvent(env event.Envelope) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.streamEvents = append(c.streamEvents, env)
	if len(c.streamEvents) > maxStreamEvents {
		c.streamEvents = c.streamEvents[len(c.streamEvents)-maxStreamEvents:]
	}
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

// Click maps a mouse click at (x,y) to a chrome action:
//   - a tab-bar hit switches the active tab (applied here -> changed=true);
//   - a sub-tab hit returns the session id the caller should focus;
//   - the "+ new" hit returns newSession=true (caller spawns a session).
//
// Body clicks return zero.
func (c *Compositor) Click(x, y int) (changed bool, focusSessID string, newSession bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	switch y {
	case rowTabBar:
		for _, sp := range c.tabSpans {
			if x >= sp.lo && x < sp.hi {
				if c.active != sp.idx {
					c.active = sp.idx
					return true, "", false
				}
				return false, "", false
			}
		}
	case rowSubTabs:
		if c.newSpan.lo >= 0 && x >= c.newSpan.lo && x < c.newSpan.hi {
			return false, "", true
		}
		for _, sp := range c.subSpans {
			if x >= sp.lo && x < sp.hi {
				if sp.idx >= 0 && sp.idx < len(c.subs) {
					return false, c.subs[sp.idx].ID, false
				}
			}
		}
	}
	return false, "", false
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
	switch {
	case c.active == 0:
		curX, curY, curVis = c.renderSessionBody(w, h)
	case tabNames[c.active] == "Stream":
		c.renderStreamBody(w, h)
	default:
		c.renderPlaceholderBody(w, h)
	}

	c.renderStatusLine(w, h)
	s.Flush(curX, curY, curVis)
}

func (c *Compositor) renderTabBar(w int) {
	c.tabSpans = c.tabSpans[:0]
	x := 0
	for i, name := range tabNames {
		label := " " + name + " "
		active := i == c.active
		fg := colDim
		if active {
			fg = colText
		}
		c.screen.SetString(x, rowTabBar, label, fg, vt.DefaultBG, active, active)
		c.tabSpans = append(c.tabSpans, span{lo: x, hi: x + len(label), idx: i})
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
	c.subSpans = c.subSpans[:0]
	c.newSpan = span{lo: -1, hi: -1, idx: -1}
	x := 0
	if len(c.subs) == 0 {
		c.screen.SetString(x, rowSubTabs, " (no sessions) ", colDim, vt.DefaultBG, false, false)
		x += len(" (no sessions) ")
	}
	for i, st := range c.subs {
		start := x
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
		c.subSpans = append(c.subSpans, span{lo: start, hi: x, idx: i})
		if x >= w {
			break
		}
	}
	// "+ new" — click (or Ctrl-G c) to start another claude session.
	label := " + new "
	if x+len(label) <= w {
		c.screen.SetString(x, rowSubTabs, label, colAccent, vt.DefaultBG, false, false)
		c.newSpan = span{lo: x, hi: x + len(label), idx: -1}
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
	// vt10x stores one logical cell per rune (no spacer after a wide rune), but a
	// wide rune (emoji/CJK) draws two columns. Map logical cells -> visual
	// columns by accumulating rune widths so alignment matches what claude drew.
	for y := 0; y < ph && y < bodyH; y++ {
		vx := 0
		for x := 0; x < pw && vx < w; x++ {
			g := p.Cell(x, y)
			ch := g.Char
			if ch == 0 {
				ch = ' '
			}
			rw := runewidth.RuneWidth(ch)
			if rw < 1 {
				rw = 1
			}
			c.screen.Set(vx, bodyTop+y, Cell{Ch: ch, FG: g.FG, BG: g.BG})
			if rw == 2 && vx+1 < w {
				c.screen.Set(vx+1, bodyTop+y, Cell{Ch: ' ', FG: g.FG, BG: g.BG, WideCont: true})
			}
			vx += rw
		}
	}
	// Map the cursor's logical column to its visual column the same way.
	cx, cy, vis := p.Cursor()
	vcx := 0
	for i := 0; i < cx && i < pw; i++ {
		rw := runewidth.RuneWidth(p.Cell(i, cy).Char)
		if rw < 1 {
			rw = 1
		}
		vcx += rw
	}
	return vcx, bodyTop + cy, vis
}

// renderStreamBody draws the live event feed (the Stream tab): the most recent
// events that fit the body region, oldest first so the newest sits at the
// bottom. Each line is "<kind>  <detail>", the kind tinted so the column scans.
func (c *Compositor) renderStreamBody(w, h int) {
	if len(c.streamEvents) == 0 {
		c.screen.SetString(2, bodyTop, "No events yet", colDim, vt.DefaultBG, false, false)
		return
	}
	rows := h - chromeRows
	if rows < 1 {
		rows = 1
	}
	start := 0
	if len(c.streamEvents) > rows {
		start = len(c.streamEvents) - rows
	}
	y := bodyTop
	for _, env := range c.streamEvents[start:] {
		kind := string(env.Event.Kind)
		c.screen.SetString(2, y, kind, colGreen, vt.DefaultBG, false, false)
		if detail := describeEvent(env.Event); detail != "" {
			c.screen.SetString(2+len(kind)+2, y, detail, colDim, vt.DefaultBG, false, false)
		}
		y++
	}
}

// describeEvent renders a one-line human summary of an event's payload, parallel
// to the desktop Stream panel's describe(). The event kind is shown separately
// by the caller, so this returns only the trailing detail (may be empty).
func describeEvent(e event.Event) string {
	switch e.Kind {
	case event.KindSessionStart:
		return e.Name + " started"
	case event.KindSessionRename:
		return "renamed → " + e.Name
	case event.KindToolCall:
		if e.ArgsSummary != "" {
			return e.Tool + " " + e.ArgsSummary
		}
		return e.Tool
	case event.KindToolResult:
		return e.Summary
	case event.KindUserMsg:
		return "user message"
	case event.KindAssistantMsg:
		return "assistant message"
	case event.KindCostTick:
		return "cost tick"
	case event.KindStatusChange:
		return string(e.From) + " → " + string(e.To)
	default:
		return ""
	}
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
	hint := "click tabs · + new session · ⌃G d detach "
	c.screen.SetString(w-len(hint), row, hint, colDim, colBarBG, false, false)
}
