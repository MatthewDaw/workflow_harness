package daemon

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"time"
)

// The attach protocol is a newline-delimited JSON control channel plus raw PTY
// byte passthrough. A client connects to the daemon's Unix socket and sends a
// Hello frame; the daemon replies with a HelloAck and then bidirectionally
// proxies PTY bytes for the focused session. Control frames (focus, resize,
// ping) are interleaved on the same connection using length-tagged framing.

// FrameType discriminates control frames on the attach channel.
type FrameType string

const (
	FramePing    FrameType = "ping"    // client -> daemon liveness probe
	FramePong    FrameType = "pong"    // daemon -> client liveness reply (carries session count)
	FrameHello   FrameType = "hello"   // client -> daemon attach handshake
	FrameAck     FrameType = "ack"     // daemon -> client handshake reply
	FrameFocus   FrameType = "focus"   // client -> daemon switch focused session
	FrameResize  FrameType = "resize"  // client -> daemon SIGWINCH dimensions
	FrameNewSess FrameType = "new"     // client -> daemon spawn a session
	FrameInput   FrameType = "input"   // client -> daemon PTY stdin bytes (base64)
	FrameOutput  FrameType = "output"  // daemon -> client PTY stdout bytes (base64)
	FrameDetach  FrameType = "detach"  // client -> daemon clean detach (daemon keeps running)
	FrameSessLs  FrameType = "sessls"  // client -> daemon list sessions
	FrameSessAck FrameType = "sessack" // daemon -> client session list reply
)

// Frame is a single control message on the attach channel.
type Frame struct {
	Type     FrameType `json:"type"`
	Sessions int       `json:"sessions,omitempty"` // pong: live session count
	SessID   string    `json:"sessId,omitempty"`   // focus/input/output target
	Data     string    `json:"data,omitempty"`     // base64 PTY bytes
	Cols     int       `json:"cols,omitempty"`     // resize
	Rows     int       `json:"rows,omitempty"`     // resize
	Ticket   string    `json:"ticket,omitempty"`   // new: optional ticket link
	Err      string    `json:"err,omitempty"`
	List     []SessInfo `json:"list,omitempty"` // sessack payload
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

// alive reports whether a daemon socket answers a ping within a short timeout.
// Used by the registry to distinguish running from stale daemons, and to clean
// up orphaned sockets.
func alive(sock string) bool {
	conn, err := net.DialTimeout("tcp", sock, 300*time.Millisecond)
	if err != nil {
		return false
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(500 * time.Millisecond))
	if err := writeFrame(conn, Frame{Type: FramePing}); err != nil {
		return false
	}
	r := bufio.NewReader(conn)
	f, err := readFrame(r)
	if err != nil {
		return false
	}
	return f.Type == FramePong
}

// pingSessions returns the live session count reported by the daemon.
func pingSessions(sock string) (int, error) {
	conn, err := net.DialTimeout("tcp", sock, 300*time.Millisecond)
	if err != nil {
		return 0, err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(500 * time.Millisecond))
	if err := writeFrame(conn, Frame{Type: FramePing}); err != nil {
		return 0, err
	}
	r := bufio.NewReader(conn)
	f, err := readFrame(r)
	if err != nil {
		return 0, err
	}
	if f.Type != FramePong {
		return 0, fmt.Errorf("unexpected reply %q", f.Type)
	}
	return f.Sessions, nil
}

// ErrDetached is returned by AttachClient.Run when the user cleanly detaches.
var ErrDetached = errors.New("detached")
