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

// errProtocolMismatch is returned by dialSock when the daemon answers the
// handshake with a different ProtocolVersion (an older/newer build). It carries
// the daemon's version for diagnostics and, crucially, lets Dial recognize the
// recoverable case and auto-replace the stale daemon rather than dead-ending.
type errProtocolMismatch struct{ daemonVersion int }

func (e errProtocolMismatch) Error() string {
	return fmt.Sprintf("daemon protocol v%d, client v%d (incompatible build)", e.daemonVersion, ProtocolVersion)
}

// Dial connects to the daemon serving repoRoot and performs the hello handshake.
// The daemon's loopback address is discovered from its registry record.
//
// Auto-recovery: a daemon left over from an older/newer build still answers a
// ping but rejects the attach handshake on a protocol mismatch. Rather than
// dead-ending the user (the historical bug, where the only escape was manually
// killing the daemon), Dial retires the stale daemon — kills its PID, removes
// its registry record + socket — spawns a fresh compatible daemon, and retries
// the attach ONCE. Net effect: `claude+` "just works" across rebuilds.
func Dial(repoRoot string) (*Client, error) {
	e, ok, err := Find(repoRoot)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, os.ErrNotExist
	}
	c, err := dialSock(e.Sock)
	if err == nil {
		return c, nil
	}
	var mismatch errProtocolMismatch
	if !errors.As(err, &mismatch) {
		return nil, err
	}
	// Recoverable: an incompatible daemon is squatting this repo. Retire it and
	// bring up a fresh one, then attach exactly once more.
	stopStale(e)
	fresh, err := EnsureDaemon(repoRoot)
	if err != nil {
		return nil, fmt.Errorf("replace incompatible daemon: %w", err)
	}
	return dialSock(fresh.Sock)
}

// DialIndex connects to the daemon at registry index n (`--session=N`). Like
// Dial, it auto-replaces an incompatible daemon and retries once.
func DialIndex(n int) (*Client, error) {
	e, err := ByIndex(n)
	if err != nil {
		return nil, err
	}
	c, err := dialSock(e.Sock)
	if err == nil {
		return c, nil
	}
	var mismatch errProtocolMismatch
	if !errors.As(err, &mismatch) {
		return nil, err
	}
	stopStale(e)
	fresh, err := EnsureDaemon(e.Repo)
	if err != nil {
		return nil, fmt.Errorf("replace incompatible daemon: %w", err)
	}
	return dialSock(fresh.Sock)
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
	// A version mismatch is reported two ways depending on build: an older daemon
	// that knows the version field returns an Err ack carrying its Version; some
	// builds simply send an ack whose Version differs. Treat either as the typed,
	// recoverable mismatch so Dial can auto-replace the daemon. Check Version
	// BEFORE the generic Err branch so a mismatch-with-error is classified
	// correctly.
	if ack.Version != ProtocolVersion {
		conn.Close()
		return nil, errProtocolMismatch{daemonVersion: ack.Version}
	}
	if ack.Err != "" {
		conn.Close()
		return nil, errors.New(ack.Err)
	}
	c.Sessions = ack.List // seed the sub-tab row before Run starts
	return c, nil
}

// SendHook delivers a raw Claude Code hook payload to the daemon serving
// repoRoot as a single one-shot frame, then closes. It is best-effort: if no
// daemon is running (or the socket is unreachable) it returns an error the
// caller ignores, so a hook never blocks a Claude Code turn (U18).
func SendHook(repoRoot string, raw []byte) error {
	e, ok, err := Find(repoRoot)
	if err != nil {
		return err
	}
	if !ok {
		return os.ErrNotExist
	}
	conn, err := net.DialTimeout("tcp", e.Sock, 1*time.Second)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(1 * time.Second))
	return writeFrame(conn, Frame{Type: FrameHook, Hook: string(raw)})
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
