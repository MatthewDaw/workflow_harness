package daemon

import (
	"github.com/workflow-harness/claude-plus/internal/pty"
	"github.com/workflow-harness/claude-plus/internal/transport"
)

// muxWriter adapts *pty.Mux to transport.SessionWriter so the control receiver
// (U15) can inject into a target session without the transport package
// importing pty (avoids a dependency cycle; the adapter lives in daemon, which
// already depends on both).
type muxWriter struct{ m *pty.Mux }

func (w muxWriter) WriteTo(sessID string, p []byte) (int, error) {
	return w.m.WriteTo(sessID, p)
}

func (w muxWriter) Get(sessID string) transport.PTYSession {
	s := w.m.Get(sessID)
	if s == nil {
		return nil
	}
	return s // *pty.Session satisfies transport.PTYSession (has Write)
}

// NewControlReceiver builds a control receiver bound to this daemon's mux.
func (d *Daemon) NewControlReceiver() *transport.Receiver {
	return transport.NewReceiver(muxWriter{m: d.mux})
}
