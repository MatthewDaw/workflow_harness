package main

import (
	"bytes"
	"strings"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/daemon"
	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/shell"
)

// TestWireClientHandlersFeedsStreamTab proves the terminal attach client is
// subscribed to the daemon's event + status frames: an event delivered via
// c.OnEvent renders on the Stream tab, and a status snapshot drives the meter.
// This is the wiring whose absence left the Stream tab a permanent placeholder.
func TestWireClientHandlersFeedsStreamTab(t *testing.T) {
	var buf bytes.Buffer
	s := shell.NewScreen(120, 30, &buf)
	comp := shell.NewCompositor(s, "test")
	c := &daemon.Client{}

	wireClientHandlers(c, comp, func() {})

	if c.OnEvent == nil {
		t.Fatal("OnEvent must be wired so the daemon's event frames reach the Stream tab")
	}
	c.OnEvent(event.Envelope{Event: event.ToolCall("s", "Grep", "foo")})
	comp.SetTab(2) // Stream
	comp.Render()
	if out := buf.String(); !strings.Contains(out, "tool.call") || !strings.Contains(out, "Grep") {
		t.Fatalf("event fed via OnEvent should render on the Stream tab; screen was:\n%s", out)
	}

	if c.OnStatus == nil {
		t.Fatal("OnStatus must be wired so the status meter reflects daemon totals")
	}
	c.OnStatus(daemon.StatusSnapshot{Tokens: 1234})
	comp.SetTab(0)
	comp.Render()
	if out := buf.String(); !strings.Contains(out, "1234tok") {
		t.Fatalf("status meter should show tokens after OnStatus; screen was:\n%s", out)
	}
}
