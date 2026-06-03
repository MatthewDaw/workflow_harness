package daemon

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"time"

	"github.com/google/uuid"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// genID returns a short unique id for an attach client connection.
func genID() string { return uuid.NewString()[:8] }

// Client is the thin attach client (the foreground `claude+` process). It dials
// the daemon socket, proxies the local terminal's stdin/stdout to the daemon's
// focused PTY, and forwards resize events. Detaching leaves the daemon running.
type Client struct {
	conn net.Conn
	r    *bufio.Reader

	// Out receives decoded PTY output bytes (tagged with the session id) for
	// rendering. Every session streams, so the client routes by id.
	Out func(sessID string, b []byte)
	// OnSessions receives session-list updates for the sub-tab row.
	OnSessions func([]SessInfo)
	// OnEvent receives captured event envelopes (the Stream panel). Optional —
	// the terminal client leaves it nil and ignores event frames.
	OnEvent func(env event.Envelope)
	// OnStatus receives meter snapshots (tokens/cost/drift). Optional.
	OnStatus func(StatusSnapshot)

	// Sessions is the session list from the attach ack (consumed during the
	// handshake, before Run starts), so the caller can seed the sub-tab row.
	Sessions []SessInfo
}

// Dial connects to the daemon serving repoRoot and performs the hello handshake.
// The daemon's loopback address is discovered from its registry record.
func Dial(repoRoot string) (*Client, error) {
	e, ok, err := Find(repoRoot)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, os.ErrNotExist
	}
	return dialSock(e.Sock)
}

// DialIndex connects to the daemon at registry index n (`--session=N`).
func DialIndex(n int) (*Client, error) {
	e, err := ByIndex(n)
	if err != nil {
		return nil, err
	}
	return dialSock(e.Sock)
}

func dialSock(sock string) (*Client, error) {
	conn, err := net.DialTimeout("tcp", sock, 2*time.Second)
	if err != nil {
		return nil, err
	}
	c := &Client{conn: conn, r: bufio.NewReader(conn)}
	if err := writeFrame(conn, Frame{Type: FrameHello, Version: ProtocolVersion}); err != nil {
		conn.Close()
		return nil, err
	}
	ack, err := readFrame(c.r)
	if err != nil {
		conn.Close()
		return nil, err
	}
	if ack.Err != "" {
		conn.Close()
		return nil, errors.New(ack.Err)
	}
	if ack.Version != ProtocolVersion {
		conn.Close()
		return nil, fmt.Errorf("daemon protocol v%d, client v%d — the daemon is an older build; restart it (kill it and re-attach)", ack.Version, ProtocolVersion)
	}
	c.Sessions = ack.List // seed the sub-tab row before Run starts
	return c, nil
}

// Input forwards local terminal stdin bytes to the daemon's focused PTY.
func (c *Client) Input(b []byte) error {
	return writeFrame(c.conn, Frame{Type: FrameInput, Data: base64.StdEncoding.EncodeToString(b)})
}

// Resize forwards a SIGWINCH to the daemon.
func (c *Client) Resize(cols, rows int) error {
	return writeFrame(c.conn, Frame{Type: FrameResize, Cols: cols, Rows: rows})
}

// Focus switches the daemon's focused session.
func (c *Client) Focus(sessID string) error {
	return writeFrame(c.conn, Frame{Type: FrameFocus, SessID: sessID})
}

// NewSession asks the daemon to spawn a session.
func (c *Client) NewSession() error {
	return writeFrame(c.conn, Frame{Type: FrameNewSess})
}

// Detach cleanly detaches, leaving the daemon (and its sessions) running.
func (c *Client) Detach() error {
	err := writeFrame(c.conn, Frame{Type: FrameDetach})
	c.conn.Close()
	return err
}

// Run reads frames from the daemon and dispatches output/session updates until
// the connection closes. Call Out/OnSessions before Run. Returns ErrDetached on
// a clean local detach (the caller decides; Run itself returns on conn close).
func (c *Client) Run() error {
	for {
		f, err := readFrame(c.r)
		if err != nil {
			return err
		}
		switch f.Type {
		case FrameOutput:
			if c.Out != nil {
				b, _ := base64.StdEncoding.DecodeString(f.Data)
				c.Out(f.SessID, b)
			}
		case FrameAck, FrameSessAck:
			if c.OnSessions != nil && f.List != nil {
				c.OnSessions(f.List)
			}
		case FrameEvent:
			if c.OnEvent != nil {
				var env event.Envelope
				if json.Unmarshal([]byte(f.EvJSON), &env) == nil {
					c.OnEvent(env)
				}
			}
		case FrameStatus:
			if c.OnStatus != nil && f.Status != nil {
				c.OnStatus(*f.Status)
			}
		}
	}
}
