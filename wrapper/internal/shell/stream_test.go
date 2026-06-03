package shell

import (
	"bytes"
	"strings"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// TestStreamTabRendersFedEvents proves the Stream tab is a live event feed:
// envelopes fed via FeedEvent are rendered (newest visible) when the Stream tab
// is active, instead of the "wiring is next" placeholder.
func TestStreamTabRendersFedEvents(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")

	c.FeedEvent(event.Envelope{Event: event.ToolCall("sess-1", "Read", "parse.go")})
	c.FeedEvent(event.Envelope{Event: event.AssistantMsg("sess-1", 42)})

	c.SetTab(2) // Stream
	c.Render()

	out := buf.String()
	if !strings.Contains(out, "tool.call") {
		t.Fatalf("Stream tab should render the tool.call event kind; screen was:\n%s", out)
	}
	if !strings.Contains(out, "Read") {
		t.Fatalf("Stream tab should render the tool name detail; screen was:\n%s", out)
	}
	if !strings.Contains(out, "assistant.msg") {
		t.Fatalf("Stream tab should render the assistant.msg event kind; screen was:\n%s", out)
	}
	if strings.Contains(out, "wiring is next") {
		t.Fatalf("Stream tab should no longer show the placeholder once events exist")
	}
}

// TestStreamTabPlaceholderWhenEmpty keeps the friendly empty-state: with no
// events fed, the Stream tab shows a "no events yet" hint rather than a blank
// body, mirroring the desktop panel.
func TestStreamTabPlaceholderWhenEmpty(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")

	c.SetTab(2) // Stream
	c.Render()

	if out := buf.String(); !strings.Contains(out, "No events yet") {
		t.Fatalf("empty Stream tab should show \"No events yet\"; screen was:\n%s", out)
	}
}
