package tui

import (
	"fmt"
	"strings"
)

// AgentRow is a scoped agent/skill row with its sync drift state (from U17).
type AgentRow struct {
	Kind  string // agent | skill
	Name  string
	Scope string // org | user | proj
	Drift string // in_sync | needs_push | needs_pull | differs | error
}

// AgentsTab shows the scoped Agents/Skills registry and local sync drift.
type AgentsTab struct {
	rows []AgentRow
}

// NewAgentsTab creates an empty agents tab.
func NewAgentsTab() AgentsTab { return AgentsTab{} }

// SetRows replaces the agent/skill rows (from a sync diff command).
func (t *AgentsTab) SetRows(rows []AgentRow) { t.rows = rows }

// View renders agents/skills grouped by scope with drift markers.
func (t AgentsTab) View(degraded bool) string {
	if degraded {
		return "⚠ HQ unreachable — showing local ~/.claude only"
	}
	if len(t.rows) == 0 {
		return "(no agents or skills)"
	}
	var b strings.Builder
	for _, r := range t.rows {
		marker := driftMarker(r.Drift)
		fmt.Fprintf(&b, "%s %-6s %-20s [%s]\n", marker, r.Kind, r.Name, r.Scope)
	}
	b.WriteString("\n[r] reconcile local ↔ HQ")
	return b.String()
}

func driftMarker(d string) string {
	switch d {
	case "needs_push":
		return "↑"
	case "needs_pull":
		return "↓"
	case "differs":
		return "⚠"
	case "error":
		return "✗"
	default:
		return "✓"
	}
}
