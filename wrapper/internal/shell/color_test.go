package shell

import (
	"bytes"
	"strings"
	"testing"
)

// TestTruecolorRendered proves the compositor emits a real 24-bit color clause
// (38;2;r;g;b) for a truecolor cell, not an invalid 256-color code — the bug
// that swallowed claude's colors.
func TestTruecolorRendered(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(20, 10, &buf) // tall enough for a body region
	c := NewCompositor(s, "x")
	c.SetSubs(nil, -1)
	// Feed a truecolor 'A' (RGB 255,128,0) into the focused pane.
	p := c.EnsurePane("only")
	p.Write([]byte("\x1b[38;2;255;128;0mA"))
	c.Render()

	out := buf.String()
	if !strings.Contains(out, "38;2;255;128;0") {
		t.Fatalf("expected a 24-bit color clause 38;2;255;128;0 in output; got:\n%q", out)
	}
	// And must NOT emit the invalid 256-color form for the packed value.
	if strings.Contains(out, "38;5;16744448") {
		t.Fatal("emitted an invalid 256-color code for a truecolor value")
	}
}
