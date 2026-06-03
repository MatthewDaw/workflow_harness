package daemon

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
	"time"

	"github.com/workflow-harness/claude-plus/internal/capture"
	"github.com/workflow-harness/claude-plus/internal/diag"
	"github.com/workflow-harness/claude-plus/internal/event"
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

	evMu       sync.Mutex
	eventSinks map[string]func(event.Envelope) // local event subscribers by id
	recent     []event.Envelope                // bounded replay buffer for new subscribers

	statusMu sync.Mutex
	sTokens  int64   // cumulative tokens (from cost.tick)
	sUSD     float64 // cumulative cost USD (sum of cost.tick deltas)
	sDrift   int     // agents/skills out of sync (set by the config layer)

	hookMu     sync.Mutex
	hookIngest func(sessID string, e event.Event) // set by the Runtime; routes hook events through emit

	stopCh chan struct{}
}

// New constructs a daemon bound to repoRoot. spawn may be nil (DefaultSpawn).
// The loopback listen address is assigned in Serve (a free port on 127.0.0.1).
func New(repoRoot string, spawn pty.SpawnFunc) (*Daemon, error) {
	return &Daemon{
		repoRoot:   repoRoot,
		started:    time.Now(),
		mux:        pty.NewMux(repoRoot, 80, 24, spawn),
		clients:    map[string]net.Conn{},
		eventSinks: map[string]func(event.Envelope){},
		stopCh:     make(chan struct{}),
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
	case FrameHook:
		// One-shot: the hook shim posts a single payload and disconnects. It must
		// never block a Claude Code turn, so we ingest and close without a reply.
		d.ingestHook(first.Hook)
		return
	case FrameHello:
		d.attach(conn, r, first.Version)
	default:
		_ = writeFrame(conn, Frame{Type: FrameAck, Err: "expected hello"})
	}
}

// SetHookIngestor registers the callback the Runtime uses to route a hook-sourced
// event through the same emit path as the transcript tailer (local bus + HQ).
// Until set (no Runtime), hook events fall back to a local-only publish.
func (d *Daemon) SetHookIngestor(fn func(sessID string, e event.Event)) {
	d.hookMu.Lock()
	d.hookIngest = fn
	d.hookMu.Unlock()
}

// ingestHook parses a Claude Code hook payload, maps it to a status.change event
// (via the capture layer), and publishes it. Malformed payloads and no-op hook
// kinds (PostToolUse) are dropped silently — the daemon stays stable and the
// transcript tailer remains the authoritative event source.
func (d *Daemon) ingestHook(raw string) {
	if raw == "" {
		return
	}
	var h capture.HookEvent
	if err := json.Unmarshal([]byte(raw), &h); err != nil || h.SessionID == "" {
		return
	}
	// Seed the prior status from the live session so the mapped transition starts
	// from where the session actually is, not a guess.
	var prev event.Status
	if s := d.mux.Get(h.SessionID); s != nil {
		prev = event.Status(string(s.Status()))
	}
	ev, ok := capture.MapHook(h, prev)
	if !ok {
		return
	}
	d.hookMu.Lock()
	ingest := d.hookIngest
	d.hookMu.Unlock()
	if ingest != nil {
		ingest(h.SessionID, ev)
		return
	}
	// No Runtime wired (local-only daemon): publish to the local bus directly.
	d.PublishEvent(event.Envelope{V: 1, TS: time.Now().UnixMilli(), Event: ev})
}

// attach proxies PTY I/O for an attached client until it detaches or drops. It
// first rejects a client whose protocol version does not match (the daemon may
// be an older build than the client — restart it).
func (d *Daemon) attach(conn net.Conn, r *bufio.Reader, clientVersion int) {
	if clientVersion != ProtocolVersion {
		_ = writeFrame(conn, Frame{
			Type:    FrameAck,
			Version: ProtocolVersion,
			Err: fmt.Sprintf("protocol mismatch: client v%d, daemon v%d — restart the daemon",
				clientVersion, ProtocolVersion),
		})
		return
	}
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

	// Subscribe this client to the local event stream (Stream panel). AddEventSink
	// replays the recent buffer immediately so the panel renders on open.
	d.AddEventSink(clientID, func(env event.Envelope) {
		if b, err := env.Marshal(); err == nil {
			send(Frame{Type: FrameEvent, EvJSON: string(b)})
		}
		// Status changes are driven by events, so push a fresh meter snapshot
		// alongside each forwarded event.
		st := d.Status()
		send(Frame{Type: FrameStatus, Status: &st})
	})

	// Ensure at least one session exists when a client first attaches.
	if d.mux.Count() == 0 {
		if _, err := d.mux.Spawn(""); err != nil {
			d.mux.RemoveSink(clientID)
			_ = writeFrame(conn, Frame{Type: FrameAck, Err: err.Error()})
			return
		}
	}

	// Register this client's per-client focus + size (defaults to the first
	// session at 80x24; the client sends a resize immediately after attach).
	d.mux.RegisterClient(clientID, 80, 24)

	d.mu.Lock()
	d.clients[clientID] = conn
	d.mu.Unlock()

	defer func() {
		d.mux.RemoveSink(clientID)
		d.mux.UnregisterClient(clientID)
		d.RemoveEventSink(clientID)
		d.mu.Lock()
		delete(d.clients, clientID)
		d.mu.Unlock()
		// NB: sessions keep running — daemon survives client disconnect.
	}()

	send(Frame{Type: FrameAck, Version: ProtocolVersion, Sessions: d.mux.Count(), List: d.sessInfosFor(clientID)})
	initStatus := d.Status()
	send(Frame{Type: FrameStatus, Status: &initStatus})

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
			_, _ = d.mux.WriteForClient(clientID, data)
		case FrameFocus:
			_ = d.mux.SetClientFocus(clientID, f.SessID)
			send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
		case FrameResize:
			d.mux.SetClientSize(clientID, f.Cols, f.Rows)
		case FrameNewSess:
			if s, err := d.mux.Spawn(""); err == nil && s != nil {
				_ = d.mux.SetClientFocus(clientID, s.ID)
			}
			send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
		case FrameSessLs:
			send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
		case FramePing:
			send(Frame{Type: FramePong, Sessions: d.mux.Count()})
		}
	}
}

// sessInfosFor converts the mux session views (with this client's own focus
// flag) into the wire SessInfo list.
func (d *Daemon) sessInfosFor(clientID string) []SessInfo {
	views := d.mux.ListFor(clientID)
	out := make([]SessInfo, len(views))
	for i, v := range views {
		out[i] = SessInfo{ID: v.ID, Name: v.Name, Focused: v.Focused, Status: v.Status}
	}
	return out
}
