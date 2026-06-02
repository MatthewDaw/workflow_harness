package shell

import (
	"bytes"
	"testing"
)

// TestWideRuneVisualMapping proves the compositor maps vt10x's dense logical
// cells (one per rune, no spacer after a wide rune) onto visual columns by
// accumulating rune widths — so an emoji occupies two columns and the following
// character is NOT dropped or overwritten.
//
// "A👋B" => screen col 0 'A', col 1 '👋', col 2 WideCont (covered), col 3 'B'.
func TestWideRuneVisualMapping(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(20, 10, &buf)
	c := NewCompositor(s, "x")
	c.SetSubs(nil, -1)
	p := c.EnsurePane("only")
	p.Write([]byte("A\U0001F44BB"))
	c.Render()

	row := bodyTop
	want := []struct {
		x        int
		ch       rune
		wideCont bool
	}{
		{0, 'A', false},
		{1, '\U0001F44B', false},
		{2, ' ', true}, // right half of the emoji — painter skips it
		{3, 'B', false},
	}
	for _, w := range want {
		got := s.cur[row][w.x]
		if got.Ch != w.ch || got.WideCont != w.wideCont {
			t.Errorf("col %d: got {Ch:%q WideCont:%v}, want {Ch:%q WideCont:%v}",
				w.x, got.Ch, got.WideCont, w.ch, w.wideCont)
		}
	}
}
