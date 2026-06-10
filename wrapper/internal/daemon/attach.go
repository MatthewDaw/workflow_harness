package daemon

import (
	"bufio"
	"encoding/json"
	"io"
	"net"
	"time"
)

// The attach protocol is a newline-delimited JSON control channel plus raw PTY
// byte passthrough. A client connects to the daemon's Unix socket and sends a
// Hello frame; the daemon replies with a HelloAck and then bidirectionally
// proxies PTY bytes for the focused session. Control frames (focus, resize,
// ping) are interleaved on the same connection using length-tagged framing.

// ProtocolVersion is the attach wire-protocol version, exchanged in the
// Hello/Ack handshake. The daemon is a detach-surviving process that may be an
// older build than a freshly-launched client, so same-build source coupling
// cannot catch wire drift — the handshake does: on mismatch the client is told
// to restart the daemon rather than silently misbehaving.
//
// v2 added FrameRename (the inline session-rename control channel); a stale v1
// daemon is auto-replaced on the next attach rather than silently lacking it.
// v3 added FrameKill (force-close one session) and FrameShutdown (terminate all
// sessions and stop the daemon on quit); a stale v2 daemon is auto-replaced on
// the next attach.
// v4 changed attach behavior: the daemon now pushes a fresh FrameSessAck session
// list to attached clients on every session.rename (manual ⌃R, first-prompt
// auto-name, and LLM title) so the sub-tab strip updates live instead of staying
// stale. Bumping forces a still-running v3 daemon (an old rebuild) to be detected
// as incompatible and auto-replaced on the next `claude+` launch, so the fix
// actually takes effect without the user manually killing the daemon.
const ProtocolVersion = 4

// FrameType discriminates control frames on the attach channel.
type FrameType string

const (
	FramePing     FrameType = "ping"     // client -> daemon liveness probe
	FramePong     FrameType = "pong"     // daemon -> client liveness reply (carries session count + protocol version)
	FrameHello    FrameType = "hello"    // client -> daemon attach handshake
	FrameAck      FrameType = "ack"      // daemon -> client handshake reply
	FrameFocus    FrameType = "focus"    // client -> daemon switch focused session
	FrameResize   FrameType = "resize"   // client -> daemon SIGWINCH dimensions
	FrameNewSess  FrameType = "new"      // client -> daemon spawn a session
	FrameRename   FrameType = "rename"   // client -> daemon manual session rename
	FrameKill     FrameType = "kill"     // client -> daemon force-close one session (uses SessID)
	FrameShutdown FrameType = "shutdown" // client -> daemon terminate all sessions + stop daemon
	FrameInput    FrameType = "input"    // client -> daemon PTY stdin bytes (base64)
	FrameOutput   FrameType = "output"   // daemon -> client PTY stdout bytes (base64)
	FrameDetach   FrameType = "detach"   // client -> daemon clean detach (daemon keeps running)
	FrameSessLs   FrameType = "sessls"   // client -> daemon list sessions
	FrameSessAck  FrameType = "sessack"  // daemon -> client session list reply
	FrameEvent    FrameType = "event"    // daemon -> client captured event envelope (JSON)
	FrameStatus   FrameType = "status"   // daemon -> client meter snapshot (tokens/cost/drift)
	FrameHook     FrameType = "hook"     // hook shim -> daemon Claude Code hook payload (JSON)
)

// Frame is a single control message on the attach channel.
type Frame struct {
	Type     FrameType       `json:"type"`
	Version  int             `json:"v,omitempty"`        // hello/ack: ProtocolVersion
	Sessions int             `json:"sessions,omitempty"` // pong: live session count
	SessID   string          `json:"sessId,omitempty"`   // focus/input/output/rename target
	Name     string          `json:"name,omitempty"`     // rename: new session name
	Data     string          `json:"data,omitempty"`     // base64 PTY bytes
	Cols     int             `json:"cols,omitempty"`     // resize
	Rows     int             `json:"rows,omitempty"`     // resize
	Err      string          `json:"err,omitempty"`
	List     []SessInfo      `json:"list,omitempty"`   // sessack payload
	EvJSON   string          `json:"ev,omitempty"`     // event: marshaled event.Envelope
	Status   *StatusSnapshot `json:"status,omitempty"` // status: meter snapshot
	Hook     string          `json:"hook,omitempty"`   // hook: raw Claude Code hook payload (JSON)
}

// SessInfo is the public view of a session for the client's sub-tab row.
type SessInfo struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Focused bool   `json:"focused"`
	Status  string `json:"status"`
}

// writeFrame encodes a frame as a single JSON line.
func writeFrame(w io.Writer, f Frame) error {
	b, err := json.Marshal(f)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	_, err = w.Write(b)
	return err
}

// readFrame decodes a single JSON line into a frame.
func readFrame(r *bufio.Reader) (Frame, error) {
	line, err := r.ReadBytes('\n')
	if err != nil {
		return Frame{}, err
	}
	var f Frame
	if err := json.Unmarshal(line, &f); err != nil {
		return Frame{}, err
	}
	return f, nil
}

// probe sends a single ping and returns the daemon's pong (its ProtocolVersion
// and session count). ok is false when the socket does not answer a well-formed
// pong within a short timeout (dead/stale daemon, or a non-claude+ listener that
// happens to hold the port). This is the single low-level liveness primitive;
// alive and compatible are thin wrappers over it so the wire behavior (and
// timeouts) stay consistent.
func probe(sock string) (pong Frame, ok bool) {
	conn, err := net.DialTimeout("tcp", sock, 300*time.Millisecond)
	if err != nil {
		return Frame{}, false
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(500 * time.Millisecond))
	if err := writeFrame(conn, Frame{Type: FramePing}); err != nil {
		return Frame{}, false
	}
	f, err := readFrame(bufio.NewReader(conn))
	if err != nil || f.Type != FramePong {
		return Frame{}, false
	}
	return f, true
}

// alive reports whether a daemon socket answers a ping within a short timeout.
// Used by the registry to distinguish running from stale daemons, and to clean
// up orphaned sockets. It does NOT check protocol compatibility — a still-running
// older-build daemon is "alive" but not "compatible"; use compatible for that.
func alive(sock string) bool {
	_, ok := probe(sock)
	return ok
}

// compatible reports whether the daemon at sock answers a ping AND speaks this
// client's ProtocolVersion. A daemon that is alive but reports a different
// version is an older/newer build that must be replaced before attaching — this
// is what lets `claude+` auto-recover across rebuilds without a full attach
// round-trip and without the user manually killing anything.
func compatible(sock string) bool {
	f, ok := probe(sock)
	return ok && f.Version == ProtocolVersion
}
