package transport

import (
	"fmt"
	"time"
)

// ControlAction is an HQ-originated steering command (U7 → U15).
type ControlAction string

const (
	ActionInject    ControlAction = "inject"    // write payload text to the session PTY stdin
	ActionPause     ControlAction = "pause"     // signal the session to pause (SIGTSTP-style)
	ActionInterrupt ControlAction = "interrupt" // send an interrupt (Ctrl-C) to the session
	ActionShutdown  ControlAction = "shutdown"  // terminate gracefully (SIGTERM, force-fallback)
	ActionKill      ControlAction = "kill"      // terminate immediately (force)
)

// shutdownGrace is how long a graceful `shutdown` waits for the child to exit
// after SIGTERM before escalating to a force kill.
const shutdownGrace = 5 * time.Second

// ControlFrame is a control message routed down the outbound WS to this daemon.
type ControlFrame struct {
	SessionID string        `json:"sessionId"`
	Action    ControlAction `json:"action"`
	Payload   string        `json:"payload,omitempty"` // inject text
}

// SessionWriter is the minimal capability the control receiver needs from the
// PTY mux: write bytes to a specific session, addressable by id. *pty.Mux
// satisfies this via WriteTo.
type SessionWriter interface {
	WriteTo(sessID string, p []byte) (int, error)
	Get(sessID string) PTYSession
}

// PTYSession is the per-session capability for signal-style and lifecycle
// controls. *pty.Session satisfies it (Write for inject/signal bytes, Shutdown
// for graceful terminate, Close for force terminate).
type PTYSession interface {
	Write(p []byte) (int, error)
	Shutdown(timeout time.Duration) error
	Close() error
}

// ctrlC is the byte sequence for an interrupt sent to a PTY (ETX).
const ctrlC = "\x03"

// Receiver applies control frames to the correct session. It targets by
// sessionId so a background session can be steered without stealing focus
// (U15: targeting). Unknown/closed sessions are dropped with an error (NACK),
// keeping the daemon stable.
type Receiver struct {
	mux SessionWriter
	// Nack, if set, is called when a frame cannot be applied (for surfacing to
	// HQ / logging). Returning the error lets the WS layer NACK upstream.
	Nack func(ControlFrame, error)
	// Terminated, if set, is invoked with the sessionId after a shutdown/kill
	// control is applied — for BOTH a known session that was actually torn down
	// AND an unknown/ghost session that this daemon never hosted. It is the hook
	// the daemon uses to emit a terminal `status.change -> done` event to HQ so
	// the live row clears. Without it, a successful shutdown left HQ believing the
	// session was still live (it never learned the session ended), which the user
	// experienced as "I clicked force shut down and nothing happened". Emitting
	// done for ghosts is intentional: instanceId is stable per device, so a
	// shutdown for a session from an earlier dead daemon routes to the live daemon
	// that never had it — the row must still disappear, so we emit done anyway.
	Terminated func(sessionID string)
}

// NewReceiver wires a receiver to the session mux.
func NewReceiver(mux SessionWriter) *Receiver { return &Receiver{mux: mux} }

// Handle applies a single control frame. It is the ControlHandler passed to the
// WS Client. Errors are reported via Nack but never panic the daemon.
func (r *Receiver) Handle(f ControlFrame) {
	if err := r.apply(f); err != nil {
		if r.Nack != nil {
			r.Nack(f, err)
		}
	}
}

// emitTerminated invokes the Terminated hook if set. Centralized so every
// shutdown/kill path (known + ghost) notifies HQ identically.
func (r *Receiver) emitTerminated(sessionID string) {
	if r.Terminated != nil {
		r.Terminated(sessionID)
	}
}

func (r *Receiver) apply(f ControlFrame) error {
	if f.SessionID == "" {
		return fmt.Errorf("control: missing sessionId")
	}
	switch f.Action {
	case ActionInject:
		payload := f.Payload
		// Submit the injected text as a turn by appending a newline if absent.
		if len(payload) == 0 || payload[len(payload)-1] != '\n' {
			payload += "\n"
		}
		_, err := r.mux.WriteTo(f.SessionID, []byte(payload))
		return err
	case ActionInterrupt:
		_, err := r.mux.WriteTo(f.SessionID, []byte(ctrlC))
		return err
	case ActionPause:
		// Pause is modeled as a no-op write of a pause sentinel; the PTY child
		// (claude) interprets repeated interrupts as a halt. We send a single
		// interrupt to stop the current turn without killing the session.
		_, err := r.mux.WriteTo(f.SessionID, []byte(ctrlC))
		return err
	case ActionShutdown:
		// Graceful terminate: SIGTERM, escalating to a force kill if the child
		// doesn't exit within the grace window (handled inside Shutdown).
		sess := r.mux.Get(f.SessionID)
		if sess == nil {
			// Ghost: a session this daemon never hosted (stable per-device
			// instanceId routes an old daemon's sessions here). Still tell HQ it's
			// done so the live row the user clicked actually disappears.
			r.emitTerminated(f.SessionID)
			return fmt.Errorf("control: no session %q", f.SessionID)
		}
		err := sess.Shutdown(shutdownGrace)
		// The child is now gone; notify HQ so the live row clears. We emit even on
		// a Shutdown error so a partially-failed teardown still retires the row.
		r.emitTerminated(f.SessionID)
		return err
	case ActionKill:
		// Immediate force terminate.
		sess := r.mux.Get(f.SessionID)
		if sess == nil {
			r.emitTerminated(f.SessionID)
			return fmt.Errorf("control: no session %q", f.SessionID)
		}
		err := sess.Close()
		r.emitTerminated(f.SessionID)
		return err
	default:
		return fmt.Errorf("control: unknown action %q", f.Action)
	}
}
