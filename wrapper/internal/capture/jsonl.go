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
	"sync"
	"time"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// transcriptLine is the subset of a CURRENT Claude Code transcript JSONL row we
// read. The transcript is an append-only JSONL; each line is one record. The
// exact schema is pinned by the recorded fixture in testdata and isolated here
// so a Claude Code format change is contained (R2).
//
// Current schema (claude-plus, CLI v2.1.x), the ground truth this parses:
//   - A row's discriminator is `type`: "user", "assistant", and many control
//     rows ("attachment", "system", "mode", "permission-mode", "ai-title",
//     "last-prompt", "queue-operation", "file-history-snapshot", …) we ignore.
//   - user / assistant rows nest the real payload under `message`. The
//     `message.content` is EITHER a plain string (a user prompt) OR an ARRAY of
//     content blocks. Each block has a `type`:
//       text        → {type:"text", text:"…"}                 (user/assistant)
//       thinking     → {type:"thinking", …}                   (assistant; skipped)
//       tool_use     → {type:"tool_use", id, name, input:{…}} (assistant)
//       tool_result  → {type:"tool_result", tool_use_id, content, is_error}
//                                                              (carried on USER rows)
//   - assistant rows carry server usage at `message.usage` (input/output tokens).
//
// So a single assistant row may expand into multiple events (text + N tool_use),
// and a user row into either a prompt event or N tool_result events. handleLine
// walks `message.content[]` and emits ONE event per meaningful block.
type transcriptLine struct {
	Type    string          `json:"type"`
	Message json.RawMessage `json:"message"`
}

// messageEnvelope is the nested `message` object on user/assistant rows.
type messageEnvelope struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"` // string OR []block
	Usage   *usage          `json:"usage"`
}

// block is one element of a `message.content` array. The union is wide; we read
// the fields relevant to each block type and ignore the rest.
type block struct {
	Type string          `json:"type"`
	Text string          `json:"text"` // text
	// tool_use
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`
	// tool_result
	ToolUseID string          `json:"tool_use_id"`
	IsError   bool            `json:"is_error"`
	Content   json.RawMessage `json:"content"` // string OR [{type:text,text}]
}

type usage struct {
	InputTokens  int64 `json:"input_tokens"`
	OutputTokens int64 `json:"output_tokens"`
}

// EventSink consumes emitted envelopes (the transport buffer in production).
type EventSink func(event.Event)

// FirstTurnSink is notified of a session's first user turn text (for auto-name).
type FirstTurnSink func(sessID, text string)

// FirstExchangeSink is notified once with a session's first full exchange (first
// user turn + first assistant reply) so the caller can generate a richer title.
type FirstExchangeSink func(sessID, userText, assistantText string)

// Tailer follows a single session's transcript JSONL and emits events. It keeps
// a monotonic byte offset so it never double-emits a previously-seen line, and
// it tolerates a partial trailing line (it only emits complete lines).
type Tailer struct {
	sessID string
	// mu guards path, offset and leftover: Poll advances them on the tailer
	// goroutine while Repoint rewrites them from another goroutine (the daemon,
	// on a post-/resume id divergence).
	mu            sync.Mutex
	path          string
	offset        int64
	leftover      []byte
	totalUsd      float64
	sawFirst      bool
	firstUserText string
	sawAssistant  bool
	emit          EventSink
	onFirst       FirstTurnSink
	onExchange    FirstExchangeSink
}

// NewTailer creates a tailer for a session transcript path.
func NewTailer(sessID, path string, emit EventSink, onFirst FirstTurnSink) *Tailer {
	return &Tailer{sessID: sessID, path: path, emit: emit, onFirst: onFirst}
}

// OnExchange registers a callback fired once with the session's first full
// exchange (first user turn + first assistant reply with text). Optional and
// nil-safe; returns t for chaining at construction.
func (t *Tailer) OnExchange(fn FirstExchangeSink) *Tailer {
	t.onExchange = fn
	return t
}

// Poll reads any new complete lines appended since the last Poll and emits the
// corresponding events. Safe to call repeatedly (the daemon polls on a ticker
// or on inotify wake).
func (t *Tailer) Poll() error {
	// Hold mu for the whole Poll body so a concurrent Repoint cannot swap the
	// path/offset out from under an in-flight read (which would corrupt the
	// offset). Poll is short and only opens/reads a local file, so this is cheap.
	t.mu.Lock()
	defer t.mu.Unlock()

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
			t.leftover = append(t.leftover[:0], chunk...)
			t.offset -= int64(len(chunk))
			break
		}
		t.leftover = nil
		if len(chunk) > 0 {
			t.handleLine(strings.TrimRight(string(chunk), "\n"))
		}
		if err != nil {
			break
		}
	}
	return nil
}

// Repoint switches the watched file to path and rewinds to its start, while
// keeping sessID unchanged so emitted events stay keyed on the stable tab id.
//
// Why: after an in-session /resume, Claude's live session_id diverges from the
// launch/tab id and it begins writing a NEW transcript at <liveId>.jsonl — the
// original <tabId>.jsonl freezes. The daemon learns the live id from the hook
// and repoints this tailer at the live file so streaming continues. Starting the
// new file fresh (offset 0) is safe: downstream sequence numbers keep advancing
// and HQ's (sessionId, seq) store key dedupes any overlap, so a re-read of rows
// shared between the two files cannot create duplicates.
func (t *Tailer) Repoint(path string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.path = path
	t.offset = 0
	t.leftover = nil
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

// handleLine maps a single transcript record to zero or more events. It walks
// the current claude transcript schema (see transcriptLine): only user and
// assistant rows carry content; everything else (control/metadata rows) is
// skipped. A user/assistant row's `message.content` is either a string (a user
// prompt) or an array of blocks; each meaningful block emits its own event so HQ
// receives EVERYTHING (user text, assistant text, every tool_use, every
// tool_result) — not one coarse event per row.
func (t *Tailer) handleLine(line string) {
	line = strings.TrimSpace(line)
	if line == "" {
		return
	}
	var row transcriptLine
	if err := json.Unmarshal([]byte(line), &row); err != nil {
		return // skip unparseable lines rather than crash (R2)
	}
	if row.Type != "user" && row.Type != "assistant" {
		return // control/metadata row — nothing to forward
	}
	if len(row.Message) == 0 {
		return
	}

	var msg messageEnvelope
	if err := json.Unmarshal(row.Message, &msg); err != nil {
		return
	}

	switch row.Type {
	case "user":
		t.handleUser(msg)
	case "assistant":
		t.handleAssistant(msg)
	}
}

// handleUser emits events for a user row. The content is either a plain prompt
// string (one user.msg) or an array that may contain tool_result blocks (one
// tool.result each) and/or text blocks (one user.msg).
func (t *Tailer) handleUser(msg messageEnvelope) {
	// Plain-string content: a typed user prompt.
	if s, ok := asString(msg.Content); ok {
		text := strings.TrimSpace(s)
		if text == "" {
			return
		}
		t.firstTurn(text)
		t.emit(event.UserMsgText(t.sessID, tokenCount(text), capContent(text)))
		return
	}

	blocks := parseBlocks(msg.Content)
	for _, b := range blocks {
		switch b.Type {
		case "text":
			text := strings.TrimSpace(b.Text)
			if text == "" {
				continue
			}
			t.firstTurn(text)
			t.emit(event.UserMsgText(t.sessID, tokenCount(text), capContent(text)))
		case "tool_result":
			summary := capContent(textFromContent(b.Content))
			// Current transcript tool_result rows carry no duration; report 0ms.
			t.emit(event.ToolResult(t.sessID, !b.IsError, 0, summary))
		}
	}
}

// handleAssistant emits events for an assistant row: one assistant.msg per text
// block (carrying the real text) and one tool.call per tool_use block. thinking
// and other block types are skipped. Server usage (message.usage) attributes
// output tokens to the first text block of the turn.
func (t *Tailer) handleAssistant(msg messageEnvelope) {
	var tokens int64
	if msg.Usage != nil {
		tokens = msg.Usage.OutputTokens
	}
	blocks := parseBlocks(msg.Content)
	textEmitted := false
	for _, b := range blocks {
		switch b.Type {
		case "text":
			text := strings.TrimSpace(b.Text)
			if text == "" {
				continue
			}
			// The first textual assistant reply completes the opening exchange;
			// hand it (with the first user turn) to the title generator once.
			if !t.sawAssistant && t.firstUserText != "" {
				t.sawAssistant = true
				if t.onExchange != nil {
					t.onExchange(t.sessID, t.firstUserText, text)
				}
			}
			tok := int64(0)
			if !textEmitted {
				tok = tokens // attribute the row's output tokens to its first text block
				textEmitted = true
			}
			t.emit(event.AssistantMsgText(t.sessID, tok, capContent(text)))
		case "tool_use":
			t.emit(event.ToolCall(t.sessID, b.Name, summarizeInput(b.Input)))
		}
	}
}

// firstTurn records the session's first user text (for auto-name) exactly once.
func (t *Tailer) firstTurn(text string) {
	if t.sawFirst {
		return
	}
	t.sawFirst = true
	t.firstUserText = text
	if t.onFirst != nil && text != "" {
		t.onFirst(t.sessID, text)
	}
}
