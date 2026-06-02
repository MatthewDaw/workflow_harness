package tui

import (
	"fmt"
	"strings"
)

// Ticket is the wrapper-side view of an HQ ticket (subset of the REST DTO).
type Ticket struct {
	ID       string
	Title    string
	Status   string // backlog | in_progress | in_review | done
	Priority string
	Session  string // linked live session id, if any
}

// TicketsTab renders the project's tickets pulled from HQ REST and offers
// "start session on ticket". When HQ is unreachable it shows a degraded state.
type TicketsTab struct {
	tickets []Ticket
	cursor  int
}

// NewTicketsTab creates an empty tickets tab.
func NewTicketsTab() TicketsTab { return TicketsTab{} }

// SetTickets replaces the ticket list (called from a REST fetch command).
func (t *TicketsTab) SetTickets(ts []Ticket) {
	t.tickets = ts
	if t.cursor >= len(ts) {
		t.cursor = 0
	}
}

// Selected returns the ticket under the cursor, if any.
func (t TicketsTab) Selected() (Ticket, bool) {
	if t.cursor < 0 || t.cursor >= len(t.tickets) {
		return Ticket{}, false
	}
	return t.tickets[t.cursor], true
}

// View renders the tickets grouped by status column.
func (t TicketsTab) View(degraded bool) string {
	if degraded {
		return "⚠ HQ unreachable — tickets unavailable (Session tab still works)"
	}
	if len(t.tickets) == 0 {
		return "(no tickets — backlog empty)"
	}
	order := []string{"in_progress", "in_review", "backlog", "done"}
	byStatus := map[string][]Ticket{}
	for _, tk := range t.tickets {
		byStatus[tk.Status] = append(byStatus[tk.Status], tk)
	}
	var b strings.Builder
	for _, st := range order {
		rows := byStatus[st]
		if len(rows) == 0 {
			continue
		}
		fmt.Fprintf(&b, "%s\n", strings.ToUpper(strings.ReplaceAll(st, "_", " ")))
		for _, tk := range rows {
			live := ""
			if tk.Session != "" {
				live = " ▸live:" + tk.Session
			}
			fmt.Fprintf(&b, "  [%s] %s %s%s\n", tk.Priority, tk.ID, tk.Title, live)
		}
	}
	b.WriteString("\n[s] start session on ticket")
	return b.String()
}
