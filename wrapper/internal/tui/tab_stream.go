package tui

import "strings"

// StreamScope selects which sessions the Stream tab shows.
type StreamScope int

const (
	ScopeAll StreamScope = iota
	ScopeFocused
)

// StreamTab renders the local event feed (the envelopes the capture layer emits
// before they go to HQ). Supports an all-vs-one scope toggle and pause.
type StreamTab struct {
	lines    []string
	maxLines int
	scope    StreamScope
	paused   bool
}

// NewStreamTab creates a stream tab with bounded scrollback.
func NewStreamTab() StreamTab { return StreamTab{maxLines: 1000} }

// Append adds a feed line unless paused.
func (t *StreamTab) Append(line string) {
	if t.paused {
		return
	}
	t.lines = append(t.lines, line)
	if len(t.lines) > t.maxLines {
		t.lines = t.lines[len(t.lines)-t.maxLines:]
	}
}

// TogglePause halts/resumes the scroll.
func (t *StreamTab) TogglePause() { t.paused = !t.paused }

// SetScope switches between all sessions and the focused one.
func (t *StreamTab) SetScope(s StreamScope) { t.scope = s }

// Paused reports the pause state (for tests/status).
func (t StreamTab) Paused() bool { return t.paused }

// View renders the tail of the feed.
func (t StreamTab) View() string {
	out := t.lines
	const show = 24
	if len(out) > show {
		out = out[len(out)-show:]
	}
	header := "Stream [all]"
	if t.scope == ScopeFocused {
		header = "Stream [focused]"
	}
	if t.paused {
		header += " (paused)"
	}
	if len(out) == 0 {
		return header + "\n(waiting for events…)"
	}
	return header + "\n" + strings.Join(out, "\n")
}
