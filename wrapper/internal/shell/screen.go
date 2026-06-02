package shell

import (
	"bufio"
	"io"
	"strconv"

	vt "github.com/hinshun/vt10x"
)

// Cell is one rendered character on the composed screen.
type Cell struct {
	Ch      rune
	FG, BG  vt.Color
	Reverse bool
	Bold    bool
	// WideCont marks the right half of a wide (2-cell) character. The terminal
	// draws nothing here — the wide rune to the left already covers it — so the
	// painter skips it.
	WideCont bool
}

func blank() Cell { return Cell{Ch: ' ', FG: vt.DefaultFG, BG: vt.DefaultBG} }

// Screen is a double-buffered cell grid that renders to a terminal by diffing
// against the previously painted frame — only changed cells are emitted, so a
// fast-updating TUI stays flicker-free and cheap over SSH.
type Screen struct {
	w, h int
	cur  [][]Cell
	prev [][]Cell
	out  *bufio.Writer
}

// NewScreen builds a screen of size w×h writing to out.
func NewScreen(w, h int, out io.Writer) *Screen {
	s := &Screen{out: bufio.NewWriterSize(out, 64*1024)}
	s.Resize(w, h)
	return s
}

// Resize reallocates the buffers; the next Flush repaints everything.
func (s *Screen) Resize(w, h int) {
	if w < 1 {
		w = 1
	}
	if h < 1 {
		h = 1
	}
	s.w, s.h = w, h
	s.cur = makeGrid(w, h)
	s.prev = makeGrid(w, h)
	// Force a full repaint: clear the real screen and invalidate prev.
	for y := range s.prev {
		for x := range s.prev[y] {
			s.prev[y][x] = Cell{Ch: 0} // sentinel that never equals a real cell
		}
	}
	s.out.WriteString("\x1b[2J")
}

func makeGrid(w, h int) [][]Cell {
	g := make([][]Cell, h)
	for y := range g {
		g[y] = make([]Cell, w)
		for x := range g[y] {
			g[y][x] = blank()
		}
	}
	return g
}

// Size returns the screen dimensions.
func (s *Screen) Size() (w, h int) { return s.w, s.h }

// ClearBack resets the back buffer to blanks before composing a new frame.
func (s *Screen) ClearBack() {
	for y := range s.cur {
		for x := range s.cur[y] {
			s.cur[y][x] = blank()
		}
	}
}

// Set writes a cell into the back buffer (bounds-checked).
func (s *Screen) Set(x, y int, c Cell) {
	if x < 0 || y < 0 || x >= s.w || y >= s.h {
		return
	}
	if c.Ch == 0 {
		c.Ch = ' '
	}
	s.cur[y][x] = c
}

// SetString writes a plain (styled) string starting at (x,y), left to right.
func (s *Screen) SetString(x, y int, str string, fg, bg vt.Color, bold, reverse bool) {
	for _, r := range str {
		if x >= s.w {
			break
		}
		s.Set(x, y, Cell{Ch: r, FG: fg, BG: bg, Bold: bold, Reverse: reverse})
		x++
	}
}

type sgrState struct {
	fg, bg        vt.Color
	bold, reverse bool
	set           bool
}

// Flush diffs the back buffer against the last frame and paints only changes,
// then positions the hardware cursor.
func (s *Screen) Flush(curX, curY int, curVisible bool) {
	out := s.out
	out.WriteString("\x1b[?25l") // hide cursor while painting

	var last sgrState
	lastX, lastY := -1, -1
	for y := 0; y < s.h; y++ {
		for x := 0; x < s.w; x++ {
			c := s.cur[y][x]
			if c.WideCont {
				continue // covered by the wide rune to its left
			}
			if c == s.prev[y][x] {
				continue
			}
			if lastY != y || lastX != x {
				out.WriteString("\x1b[")
				out.WriteString(strconv.Itoa(y + 1))
				out.WriteByte(';')
				out.WriteString(strconv.Itoa(x + 1))
				out.WriteByte('H')
			}
			st := sgrState{fg: c.FG, bg: c.BG, bold: c.Bold, reverse: c.Reverse, set: true}
			if st != last {
				writeSGR(out, st)
				last = st
			}
			ch := c.Ch
			if ch == 0 {
				ch = ' '
			}
			out.WriteRune(ch)
			lastX, lastY = x+1, y
		}
	}
	out.WriteString("\x1b[m") // reset attributes

	if curVisible {
		out.WriteString("\x1b[")
		out.WriteString(strconv.Itoa(curY + 1))
		out.WriteByte(';')
		out.WriteString(strconv.Itoa(curX + 1))
		out.WriteByte('H')
		out.WriteString("\x1b[?25h")
	}
	_ = out.Flush()

	// Promote cur -> prev.
	for y := 0; y < s.h; y++ {
		copy(s.prev[y], s.cur[y])
	}
}

// writeSGR emits the SGR escape for a cell style.
func writeSGR(out *bufio.Writer, st sgrState) {
	out.WriteString("\x1b[0") // reset, then add attributes
	if st.bold {
		out.WriteString(";1")
	}
	if st.reverse {
		out.WriteString(";7")
	}
	writeColor(out, 38, st.fg) // foreground
	writeColor(out, 48, st.bg) // background
	out.WriteByte('m')
}

// writeColor appends an SGR color clause for fg (base 38) or bg (base 48).
// vt10x packs colors three ways: 0..15 ANSI, 16..255 xterm-256, and truecolor
// as a 24-bit 0xRRGGBB value (which is < DefaultFG = 1<<24). Anything >=
// DefaultFG is the terminal default and emits nothing.
func writeColor(out *bufio.Writer, base int, c vt.Color) {
	v := uint32(c)
	if v >= uint32(vt.DefaultFG) {
		return // default / special -> leave the terminal default
	}
	out.WriteByte(';')
	out.WriteString(strconv.Itoa(base))
	if v < 256 {
		out.WriteString(";5;")
		out.WriteString(strconv.Itoa(int(v)))
		return
	}
	// Truecolor: unpack 0xRRGGBB -> 38;2;r;g;b
	out.WriteString(";2;")
	out.WriteString(strconv.Itoa(int((v >> 16) & 0xFF)))
	out.WriteByte(';')
	out.WriteString(strconv.Itoa(int((v >> 8) & 0xFF)))
	out.WriteByte(';')
	out.WriteString(strconv.Itoa(int(v & 0xFF)))
}
