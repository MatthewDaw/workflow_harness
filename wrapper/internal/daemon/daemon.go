package daemon

import (
	"bufio"
	"encoding/base64"
	"net"
	"os"
	"sync"
	"time"

	"github.com/workflow-harness/claude-plus/internal/diag"
	"github.com/workflow-harness/claude-plus/internal/pty"
)

// Daemon is the per-repo background process. It owns a pty.Mux of claude
// sessions and serves attach clients over a Unix socket. It survives client
// disconnect: detaching only removes a sink, it never tears down sessions.
type Daemon struct {
	repoRoot string
	sock     string
	started  time.Time

	ln   net.Listener
	mux  *pty.Mux

	mu      sync.Mutex
	clients map[string]net.Conn // attach client connections by id

	stopCh chan struct{}
}

// New constructs a daemon bound to repoRoot. spawn may be nil (DefaultSpawn).
// The loopback listen address is assigned in Serve (a free port on 127.0.0.1).
func New(repoRoot string, spawn pty.SpawnFunc) (*Daemon, error) {
	return &Daemon{
		repoRoot: repoRoot,
		started:  time.Now(),
		mux:      pty.NewMux(repoRoot, 80, 24, spawn),
		clients:  map[string]net.Conn{},
		stopCh:   make(chan struct{}),
	}, nil
}

// Mux exposes the session multiplexer (used by capture/transport wiring).
func (d *Daemon) Mux() *pty.Mux { return d.mux }

// RepoRoot returns the daemon's repo root.
func (d *Daemon) RepoRoot() string { return d.repoRoot }

// Serve binds the socket, records the registry entry, and accepts clients until
// Stop is called. It is the daemon's main loop.
func (d *Daemon) Serve() error {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	d.ln = ln
	d.sock = ln.Addr().String() // 127.0.0.1:<assigned-port>

	if err := writeMeta(d.entry()); err != nil {
		return err
	}
	defer removeMeta(d.repoRoot)

	// Refresh the registry entry's session count periodically so `ls` is fresh
	// even without a probe connection.
	go d.refreshLoop()

	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-d.stopCh:
				return nil
			default:
				return err
			}
		}
		go d.handle(conn)
	}
}

// entry builds the current registry Entry snapshot.
func (d *Daemon) entry() Entry {
	repoName := d.repoRoot
	for i := len(repoName) - 1; i >= 0; i-- {
		if repoName[i] == '/' || repoName[i] == '\\' {
			repoName = repoName[i+1:]
			break
		}
	}
	return Entry{
		Repo:     d.repoRoot,
		RepoName: repoName,
		Host:     hostName(),
		Sessions: d.mux.Count(),
		State:    StateRunning,
		Started:  d.started,
		Sock:     d.sock,
		PID:      os.Getpid(),
	}
}

func (d *Daemon) refreshLoop() {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-d.stopCh:
			return
		case <-t.C:
			_ = writeMeta(d.entry())
		}
	}
}

// Stop shuts the daemon down: closes sessions, the listener, and the registry.
func (d *Daemon) Stop() {
	close(d.stopCh)
	if d.ln != nil {
		_ = d.ln.Close()
	}
	d.mux.CloseAll()
}

// handle serves one attach client. Liveness pings get a fast reply and close;
// a hello starts a full attach session with output fan-out.
func (d *Daemon) handle(conn net.Conn) {
	defer conn.Close()
	defer diag.Recover("daemon.handle")
	r := bufio.NewReader(conn)

	first, err := readFrame(r)
	if err != nil {
		return
	}

	switch first.Type {
	case FramePing:
		_ = writeFrame(conn, Frame{Type: FramePong, Sessions: d.mux.Count()})
		return
	case FrameHello:
		d.attach(conn, r)
	default:
		_ = writeFrame(conn, Frame{Type: FrameAck, Err: "expected hello"})
	}
}

// attach proxies PTY I/O for an attached client until it detaches or drops.
func (d *Daemon) attach(conn net.Conn, r *bufio.Reader) {
	clientID := genID()

	// Serialize writes to this client (mux fan-out is concurrent).
	var wmu sync.Mutex
	send := func(f Frame) {
		wmu.Lock()
		defer wmu.Unlock()
		_ = writeFrame(conn, f)
	}

	// Stream every session's output (tagged with its id). The client keeps a
	// mirror terminal per session and renders the focused one, so switching is
	// instant and a freshly spawned session is never blank. AddSink also replays
	// each existing session's recent output, so reattaching renders immediately.
	// Register the sink BEFORE spawning the initial session so claude's one-time
	// welcome paint is captured live rather than lost.
	d.mux.AddSink(clientID, func(sessID string, b []byte) {
		send(Frame{Type: FrameOutput, SessID: sessID, Data: base64.StdEncoding.EncodeToString(b)})
	})

	// Ensure at least one session exists when a client first attaches.
	if d.mux.Count() == 0 {
		if _, err := d.mux.Spawn("", ""); err != nil {
			d.mux.RemoveSink(clientID)
			_ = writeFrame(conn, Frame{Type: FrameAck, Err: err.Error()})
			return
		}
	}

	d.mu.Lock()
	d.clients[clientID] = conn
	d.mu.Unlock()

	defer func() {
		d.mux.RemoveSink(clientID)
		d.mu.Lock()
		delete(d.clients, clientID)
		d.mu.Unlock()
		// NB: sessions keep running — daemon survives client disconnect.
	}()

	send(Frame{Type: FrameAck, Sessions: d.mux.Count(), List: d.sessInfos()})

	for {
		f, err := readFrame(r)
		if err != nil {
			return // client dropped (SSH disconnect) — daemon lives on
		}
		switch f.Type {
		case FrameDetach:
			return
		case FrameInput:
			data, _ := base64.StdEncoding.DecodeString(f.Data)
			_, _ = d.mux.WriteFocused(data)
		case FrameFocus:
			_ = d.mux.Focus(f.SessID)
			send(Frame{Type: FrameSessAck, List: d.sessInfos()})
		case FrameResize:
			d.mux.Resize(f.Cols, f.Rows)
		case FrameNewSess:
			_, _ = d.mux.Spawn("", f.Ticket)
			send(Frame{Type: FrameSessAck, List: d.sessInfos()})
		case FrameSessLs:
			send(Frame{Type: FrameSessAck, List: d.sessInfos()})
		case FramePing:
			send(Frame{Type: FramePong, Sessions: d.mux.Count()})
		}
	}
}

// sessInfos converts the mux session views into the wire SessInfo list.
func (d *Daemon) sessInfos() []SessInfo {
	views := d.mux.List()
	out := make([]SessInfo, len(views))
	for i, v := range views {
		out[i] = SessInfo{ID: v.ID, Name: v.Name, Focused: v.Focused, Status: v.Status}
	}
	return out
}
