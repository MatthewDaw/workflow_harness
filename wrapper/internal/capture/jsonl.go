// Package capture turns Claude Code session activity into the shared event
// envelope (U14). It has two structured sources — never PTY screen-scraping:
//
//  1. The session transcript JSONL at ~/.claude/projects/<hash>/<sid>.jsonl,
//     tailed for message / tool / cost events (KTD3 source 2).
//  2. settings.json hooks (PreToolUse/PostToolUse/Stop/Notification) that POST
//     low-latency lifecycle + status.change signals to the daemon socket
//     (KTD3 source 3) — see hooks.go.
package capture

import (
	"bufio"
	"encoding/json"
	"io"
	"os"
	"strings"
	"time"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// transcriptLine is the subset of a Claude Code transcript JSONL row we read.
// Claude Code's transcript is an append-only JSONL; each line is one record.
// The exact schema is pinned by the recorded fixture in testdata and isolated
// here so a Claude Code format change is contained (R2).
type transcriptLine struct {
	Type      string          `json:"type"`      // "user" | "assistant" | "tool_use" | "tool_result" | "result"
	Role      string          `json:"role"`      // some versions use role instead of type
	Message   json.RawMessage `json:"message"`   // nested message payload
	ToolName  string          `json:"name"`      // tool_use
	ToolInput json.RawMessage `json:"input"`     // tool_use
	IsError   bool            `json:"is_error"`  // tool_result
	Content   json.RawMessage `json:"content"`   // tool_result / message content
	Usage     *usage          `json:"usage"`     // assistant / result usage
	CostUSD   *float64        `json:"costUSD"`   // result rows carry cumulative cost
	DurationMs *int64         `json:"durationMs"`
	Timestamp string          `json:"timestamp"`
}

type usage struct {
	InputTokens  int64 `json:"input_tokens"`
	OutputTokens int64 `json:"output_tokens"`
}

// EventSink consumes emitted envelopes (the transport buffer in production).
type EventSink func(event.Event)

// FirstTurnSink is notified of a session's first user turn text (for auto-name).
type FirstTurnSink func(sessID, text string)

// Tailer follows a single session's transcript JSONL and emits events. It keeps
// a monotonic byte offset so it never double-emits a previously-seen line, and
// it tolerates a partial trailing line (it only emits complete lines).
type Tailer struct {
	sessID    string
	path      string
	offset    int64
	leftover  []byte
	totalUsd  float64
	sawFirst  bool
	emit      EventSink
	onFirst   FirstTurnSink
}

// NewTailer creates a tailer for a session transcript path.
func NewTailer(sessID, path string, emit EventSink, onFirst FirstTurnSink) *Tailer {
	return &Tailer{sessID: sessID, path: path, emit: emit, onFirst: onFirst}
}

// Poll reads any new complete lines appended since the last Poll and emits the
// corresponding events. Safe to call repeatedly (the daemon polls on a ticker
// or on inotify wake).
func (t *Tailer) Poll() error {
	f, err := os.Open(t.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil // transcript not created yet
		}
		return err
	}
	defer f.Close()

	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		return err
	}
	r := bufio.NewReader(f)
	for {
		chunk, err := r.ReadBytes('\n')
		t.offset += int64(len(chunk))
		if len(chunk) > 0 && chunk[len(chunk)-1] != '\n' {
			// Partial trailing line: stash it, rewind the offset, stop. The next
			// Poll re-reads from here once the line is complete.
			t.offset -= int64(len(chunk))
			break
		}
		if len(chunk) > 0 {
			t.handleLine(strings.TrimRight(string(chunk), "\n"))
		}
		if err != nil {
			break
		}
	}
	return nil
}

// Run polls the transcript on an interval until stop is closed.
func (t *Tailer) Run(every time.Duration, stop <-chan struct{}) {
	tk := time.NewTicker(every)
	defer tk.Stop()
	for {
		select {
		case <-stop:
			_ = t.Poll() // final drain
			return
		case <-tk.C:
			_ = t.Poll()
		}
	}
}

// handleLine maps a single transcript record to zero or more events.
func (t *Tailer) handleLine(line string) {
	line = strings.TrimSpace(line)
	if line == "" {
		return
	}
	var row transcriptLine
	if err := json.Unmarshal([]byte(line), &row); err != nil {
		return // skip unparseable lines rather than crash (R2)
	}

	kind := row.Type
	if kind == "" {
		kind = row.Role
	}

	switch kind {
	case "user":
		text := extractText(row.Message, row.Content)
		if !t.sawFirst {
			t.sawFirst = true
			if t.onFirst != nil && text != "" {
				t.onFirst(t.sessID, text)
			}
		}
		t.emit(event.UserMsg(t.sessID, tokenCount(text)))
	case "assistant":
		var tokens int64
		if row.Usage != nil {
			tokens = row.Usage.OutputTokens
		}
		t.emit(event.AssistantMsg(t.sessID, tokens))
	case "tool_use":
		t.emit(event.ToolCall(t.sessID, row.ToolName, summarizeInput(row.ToolInput)))
	case "tool_result":
		var ms int64
		if row.DurationMs != nil {
			ms = *row.DurationMs
		}
		t.emit(event.ToolResult(t.sessID, !row.IsError, ms, summarizeContent(row.Content)))
	case "result":
		if row.CostUSD != nil {
			delta := *row.CostUSD - t.totalUsd
			if delta < 0 {
				delta = 0
			}
			t.totalUsd = *row.CostUSD
			var tokens int64
			if row.Usage != nil {
				tokens = row.Usage.InputTokens + row.Usage.OutputTokens
			}
			t.emit(event.CostTick(t.sessID, round2(delta), round2(t.totalUsd), tokens))
		}
	}
}
