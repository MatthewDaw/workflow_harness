package tui

import "strings"

// SessionTab embeds the focused PTY's output. The daemon streams focused-session
// bytes via PTYOutputMsg; this tab keeps a bounded scrollback for rendering.
type SessionTab struct {
	lines   []string
	pending string
	maxLines int
}

// NewSessionTab creates a session tab with a default scrollback.
func NewSessionTab() SessionTab { return SessionTab{maxLines: 2000} }

// Append feeds raw PTY bytes into the scrollback, splitting on newlines.
func (t *SessionTab) Append(b []byte) {
	t.pending += string(b)
	for {
		i := strings.IndexByte(t.pending, '\n')
		if i < 0 {
			break
		}
		t.lines = append(t.lines, strings.TrimRight(t.pending[:i], "\r"))
		t.pending = t.pending[i+1:]
	}
	if len(t.lines) > t.maxLines {
		t.lines = t.lines[len(t.lines)-t.maxLines:]
	}
}

// View renders the (tail of the) embedded PTY scrollback.
func (t SessionTab) View() string {
	out := t.lines
	const show = 24
	if len(out) > show {
		out = out[len(out)-show:]
	}
	body := strings.Join(out, "\n")
	if t.pending != "" {
		if body != "" {
			body += "\n"
		}
		body += t.pending
	}
	if body == "" {
		return "(attach a session — the live claude PTY renders here)"
	}
	return body
}
