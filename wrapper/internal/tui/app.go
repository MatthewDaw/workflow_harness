// Package tui is the wrapper's own chrome (U16): a Bubble Tea app with top tabs
// Session / Tickets / Agents / Forge / Stream, the session sub-tab row, and a
// status line. The Session tab embeds the focused PTY (proxied via the attach
// client); the other tabs talk to HQ REST or render the local feed.
package tui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// Tab identifies a top-level tab.
type Tab int

const (
	TabSession Tab = iota
	TabTickets
	TabAgents
	TabForge
	TabStream
)

var tabNames = []string{"Session", "Tickets", "Agents", "Forge", "Stream"}

// SubTab is one session in the session sub-tab row.
type SubTab struct {
	ID     string
	Name   string
	Status string
}

// Model is the root Bubble Tea model. Sub-models render each tab; the root owns
// tab selection, the status line, and window size.
type Model struct {
	width, height int
	active        Tab
	subTabs       []SubTab
	focusedSub    int

	// Status line data.
	Instance string
	Tokens   int64
	CostUSD  float64
	Drift    int // count of differing skills/agents (Agents tab warning)

	// Tab sub-models.
	session SessionTab
	tickets TicketsTab
	agents  AgentsTab
	forge   ForgeTab
	stream  StreamTab

	// degraded is set when HQ REST is unreachable; the Session tab still works.
	degraded bool

	quitting bool
}

// New constructs the root model.
func New(instance string) Model {
	return Model{
		active:   TabSession,
		Instance: instance,
		session:  NewSessionTab(),
		tickets:  NewTicketsTab(),
		agents:   NewAgentsTab(),
		forge:    NewForgeTab(),
		stream:   NewStreamTab(),
	}
}

// Init implements tea.Model.
func (m Model) Init() tea.Cmd { return nil }

// --- Messages the daemon/transport feed into the program ---

// SubTabsMsg updates the session sub-tab row.
type SubTabsMsg struct {
	Tabs    []SubTab
	Focused int
}

// PTYOutputMsg carries focused-session output bytes for the Session tab.
type PTYOutputMsg struct{ Data []byte }

// StatusMsg updates the status line meters.
type StatusMsg struct {
	Tokens  int64
	CostUSD float64
}

// StreamEventMsg appends a line to the Stream tab feed.
type StreamEventMsg struct{ Line string }

// DegradedMsg flags HQ REST connectivity loss/restore.
type DegradedMsg struct{ Down bool }

// DriftMsg updates the Agents-tab drift warning count.
type DriftMsg struct{ Count int }

// Update implements tea.Model.
func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
	case tea.KeyMsg:
		return m.handleKey(msg)
	case SubTabsMsg:
		m.subTabs = msg.Tabs
		m.focusedSub = msg.Focused
	case PTYOutputMsg:
		m.session.Append(msg.Data)
	case StatusMsg:
		m.Tokens, m.CostUSD = msg.Tokens, msg.CostUSD
	case StreamEventMsg:
		m.stream.Append(msg.Line)
	case DegradedMsg:
		m.degraded = msg.Down
	case DriftMsg:
		m.Drift = msg.Count
	}
	return m, nil
}

func (m Model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "ctrl+c", "ctrl+q":
		m.quitting = true
		return m, tea.Quit
	case "tab":
		m.active = (m.active + 1) % Tab(len(tabNames))
		return m, nil
	case "shift+tab":
		m.active = (m.active - 1 + Tab(len(tabNames))) % Tab(len(tabNames))
		return m, nil
	}
	// Number keys jump directly to a tab (1..5).
	if len(msg.String()) == 1 {
		if d := msg.String()[0] - '1'; int(d) < len(tabNames) {
			m.active = Tab(d)
			return m, nil
		}
	}
	return m, nil
}

// View implements tea.Model.
func (m Model) View() string {
	if m.quitting {
		return ""
	}
	var b strings.Builder
	b.WriteString(m.renderTabBar())
	b.WriteString("\n")
	if m.active == TabSession {
		b.WriteString(m.renderSubTabs())
		b.WriteString("\n")
	}
	b.WriteString(m.renderBody())
	b.WriteString("\n")
	b.WriteString(m.renderStatusLine())
	return b.String()
}

func (m Model) renderTabBar() string {
	var parts []string
	for i, name := range tabNames {
		style := tabInactive
		if Tab(i) == m.active {
			style = tabActive
		}
		label := name
		if Tab(i) == TabAgents && m.Drift > 0 {
			label = fmt.Sprintf("%s ⚠%d", name, m.Drift)
		}
		parts = append(parts, style.Render(label))
	}
	return strings.Join(parts, " ")
}

func (m Model) renderSubTabs() string {
	if len(m.subTabs) == 0 {
		return subTabStyle.Render("(no sessions)")
	}
	var parts []string
	for i, s := range m.subTabs {
		label := s.Name
		if s.Status == "needs_input" {
			label += " •"
		}
		style := subTabStyle
		if i == m.focusedSub {
			style = subTabActive
		}
		parts = append(parts, style.Render(label))
	}
	return strings.Join(parts, " ")
}

func (m Model) renderBody() string {
	switch m.active {
	case TabSession:
		return m.session.View()
	case TabTickets:
		return m.tickets.View(m.degraded)
	case TabAgents:
		return m.agents.View(m.degraded)
	case TabForge:
		return m.forge.View(m.degraded)
	case TabStream:
		return m.stream.View()
	}
	return ""
}

func (m Model) renderStatusLine() string {
	focused := "—"
	if m.focusedSub >= 0 && m.focusedSub < len(m.subTabs) {
		focused = m.subTabs[m.focusedSub].Name
	}
	hint := "⇥ tab  1-5 jump  ⌃R rename  ⌃C quit"
	left := fmt.Sprintf(" %s  ▸ %s  %dtok  $%.2f", m.Instance, focused, m.Tokens, m.CostUSD)
	return statusStyle.Render(left) + "  " + hintStyle.Render(hint)
}

// Active returns the active tab (exposed for tests).
func (m Model) Active() Tab { return m.active }

// Styles (lipgloss). Kept minimal; the web SPA carries the full design system.
var (
	tabActive    = lipgloss.NewStyle().Bold(true).Underline(true)
	tabInactive  = lipgloss.NewStyle().Faint(true)
	subTabActive = lipgloss.NewStyle().Bold(true)
	subTabStyle  = lipgloss.NewStyle().Faint(true)
	statusStyle  = lipgloss.NewStyle().Reverse(true)
	hintStyle    = lipgloss.NewStyle().Faint(true)
)
