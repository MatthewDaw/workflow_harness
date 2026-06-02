package tui

import (
	"fmt"
	"strings"
)

// ForgeMatch is a similar past session returned by the Forge search (U27).
type ForgeMatch struct {
	SessionID string
	Name      string
	Score     float64
	Skills    []string
}

// ForgeTab drives the semantic-search-over-history → agent-proposal flow. The
// search itself runs server-side (U27); this tab collects the description and
// renders matches + the proposed agent draft.
type ForgeTab struct {
	query    string
	matches  []ForgeMatch
	proposal string // rendered draft agent
}

// NewForgeTab creates an empty forge tab.
func NewForgeTab() ForgeTab { return ForgeTab{} }

// SetResults populates matches + proposal from a forge search response.
func (t *ForgeTab) SetResults(query string, matches []ForgeMatch, proposal string) {
	t.query, t.matches, t.proposal = query, matches, proposal
}

// View renders the search box, ranked matches, and the proposed agent.
func (t ForgeTab) View(degraded bool) string {
	if degraded {
		return "⚠ HQ unreachable — Forge search needs the cloud"
	}
	var b strings.Builder
	fmt.Fprintf(&b, "Forge: describe the agent you want\n> %s\n\n", t.query)
	if len(t.matches) == 0 {
		b.WriteString("(run a search to mine your session history)")
		return b.String()
	}
	b.WriteString("Similar sessions:\n")
	for _, m := range t.matches {
		fmt.Fprintf(&b, "  %.2f  %s  skills: %s\n", m.Score, m.Name, strings.Join(m.Skills, ", "))
	}
	if t.proposal != "" {
		b.WriteString("\nProposed agent:\n")
		b.WriteString(t.proposal)
	}
	return b.String()
}
