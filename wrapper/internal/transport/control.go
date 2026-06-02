package transport

import (
	"fmt"
)

// ControlAction is an HQ-originated steering command (U7 → U15).
type ControlAction string

const (
	ActionInject    ControlAction = "inject"    // write payload text to the session PTY stdin
	ActionPause     ControlAction = "pause"     // signal the session to pause (SIGTSTP-style)
	ActionInterrupt ControlAction = "interrupt" // send an interrupt (Ctrl-C) to the session
)

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

// PTYSession is the per-session capability for signal-style controls.
type PTYSession interface {
	Write(p []byte) (int, error)
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
	default:
		return fmt.Errorf("control: unknown action %q", f.Action)
	}
}
