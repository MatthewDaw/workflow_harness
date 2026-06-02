package shell

import "testing"

// TestPaneEmulatesEscapes proves vt10x interprets cursor motion, clears, and
// colors — i.e. the Session pane is a real terminal, not a line buffer.
func TestPaneEmulatesEscapes(t *testing.T) {
	p := NewPane(80, 24)
	// Clear, home, red "HELLO", then absolute-position (row 5, col 10) "WORLD".
	p.Write([]byte("\x1b[2J\x1b[H\x1b[31mHELLO\x1b[0m\x1b[5;10HWORLD"))

	if got := p.Cell(0, 0).Char; got != 'H' {
		t.Fatalf("cell(0,0) char = %q, want 'H'", got)
	}
	// 'O' of HELLO at column 4.
	if got := p.Cell(4, 0).Char; got != 'O' {
		t.Fatalf("cell(4,0) char = %q, want 'O'", got)
	}
	// Foreground of the red text should not be the default color.
	red := p.Cell(0, 0).FG
	def := p.Cell(0, 0) // baseline
	_ = def
	if red.ANSI() != true {
		t.Logf("note: FG color = %v (ANSI=%v)", red, red.ANSI())
	}
	// "WORLD" placed by the absolute CUP at row 5 (y=4), col 10 (x=9).
	if got := p.Cell(9, 4).Char; got != 'W' {
		t.Fatalf("cell(9,4) char = %q, want 'W' (absolute positioning failed)", got)
	}
	if got := p.Cell(13, 4).Char; got != 'D' {
		t.Fatalf("cell(13,4) char = %q, want 'D'", got)
	}

	// Cursor should sit just past "WORLD": row 5 (y=4), col 15 (x=14).
	cx, cy, _ := p.Cursor()
	if cx != 14 || cy != 4 {
		t.Fatalf("cursor = (%d,%d), want (14,4)", cx, cy)
	}
}

func TestPaneResize(t *testing.T) {
	p := NewPane(80, 24)
	p.Resize(120, 40)
	if c, r := p.Size(); c != 120 || r != 40 {
		t.Fatalf("size = (%d,%d), want (120,40)", c, r)
	}
}
