package daemon

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strings"
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

	ln  net.Listener
	mux *pty.Mux

	mu      sync.Mutex
	clients map[string]net.Conn // attach client connections by id
	// listSinks pushes a fresh session list (FrameSessAck) to each attached
	// client. Registered per attach; invoked by broadcastSessList when the session
	// list changes outside an attach loop (e.g. auto-rename/auto-title from the
	// transcript tailer or the UserPromptSubmit hook), so the CLI tab strip updates
	// immediately instead of showing a stale name. Each closure serializes its own
	// write on the client's send mutex.
	listSinks map[string]func()

	evMu       sync.Mutex
	eventSinks map[string]func(event.Envelope) // local event subscribers by id
	recent     []event.Envelope                // bounded replay buffer for new subscribers

	statusMu sync.Mutex
	sTokens  int64   // cumulative tokens (from cost.tick)
	sUSD     float64 // cumulative cost USD (sum of cost.tick deltas)
	sDrift   int     // agents/skills out of sync (set by the config layer)

	hookMu     sync.Mutex
	hookIngest func(sessID string, e event.Event) // set by the Runtime; routes hook events through emit
	// repointHook, when set by the Runtime, is notified (tabID, liveID) just BEFORE
	// ingestHook remaps a post-/resume hook's divergent live session_id back to the
	// stable tab id. The Runtime uses it to repoint the tab's transcript tailer at
	// the live <liveId>.jsonl so a resumed conversation keeps streaming.
	repointHook func(tabID, liveID string)
	// killHook, when set by the Runtime, is invoked with a session id the moment a
	// USER intentionally ends it from an attached client (FrameKill). It is the
	// signal the Runtime uses to distinguish a user-intent end — which must REMOVE
	// the session from the cross-restart resume store so the next daemon start does
	// NOT --resume a conversation the user deliberately killed — from a restart or
	// crash, which must preserve the session for continuity. The control path
	// (recv.Terminated, HQ kill/shutdown) is threaded separately in the Runtime.
	killHook func(sessID string)
	// topicHook, when set by the Runtime, forwards a turn-cycle signal (Stop /
	// UserPromptSubmit) for a tab into captureLoop, which owns the topic-focus gate
	// + judge (U6). ingestHook only has the SessionID; the gate needs the per-tab
	// tailer, transcript path and emit closure that live in captureLoop, so the
	// signal is forwarded rather than handled here. Stop still maps to its idle
	// status.change inline (the topic gate is a side channel, never on the turn's
	// critical path).
	topicHook func(sig TopicSignal)

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
		listSinks:  map[string]func(){},
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
		Version:  ProtocolVersion,
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

// Stop shuts the daemon down: closes sessions, the listener, and removes the
// registry record + socket. It is safe to call more than once (the stop channel
// guard makes the teardown idempotent) and cleans up the registry json eagerly
// so a stopped daemon never lingers as a stale `ls` row or wedges the next
// attach-or-create. (Serve also removes the record on its own exit; doing it
// here too means cleanup happens even if Stop races ahead of Serve's defer or
// Serve was never the one to exit the accept loop.)
func (d *Daemon) Stop() {
	select {
	case <-d.stopCh:
		// already stopped
	default:
		close(d.stopCh)
	}
	if d.ln != nil {
		_ = d.ln.Close()
	}
	d.mux.CloseAll()
	_ = removeMeta(d.repoRoot)
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
		// Pong carries this daemon's ProtocolVersion so a client can detect a
		// build/wire mismatch (and replace the daemon) BEFORE committing to a full
		// attach handshake.
		_ = writeFrame(conn, Frame{Type: FramePong, Version: ProtocolVersion, Sessions: d.mux.Count()})
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

// emitSession publishes a daemon-originated session event (e.g. a manual
// rename). It routes through the Runtime's emit path when wired — so the event
// is sequenced and forwarded to HQ identically to tailer/hook events — and falls
// back to a local-only publish otherwise. Mirrors ingestHook's routing.
func (d *Daemon) emitSession(sid string, e event.Event) {
	d.hookMu.Lock()
	ingest := d.hookIngest
	d.hookMu.Unlock()
	if ingest != nil {
		ingest(sid, e)
		return
	}
	d.PublishEvent(event.Envelope{V: 1, TS: time.Now().UnixMilli(), Event: e})
}

// broadcastSessList pushes a fresh session list (FrameSessAck) to every attached
// client. Used after a session-list-affecting change that happens OUTSIDE an
// attach loop — auto-rename and auto-title from the transcript tailer, or the
// UserPromptSubmit hook — so the CLI tab strip reflects the new name immediately
// (the manual FrameRename path acks its own client inline; this covers the rest).
// Best-effort and non-blocking: each sink serializes on its own client's write
// mutex; a slow/broken client cannot stall the caller meaningfully.
func (d *Daemon) broadcastSessList() {
	d.mu.Lock()
	sinks := make([]func(), 0, len(d.listSinks))
	for _, s := range d.listSinks {
		sinks = append(sinks, s)
	}
	d.mu.Unlock()
	for _, s := range sinks {
		s()
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

// SetKillHook registers the callback FrameKill invokes when a user intentionally
// ends a session from an attached client. The Runtime uses it to remove the
// session from the cross-restart resume store (so a deliberately-killed session
// is not --resume-d on the next daemon start). Until set (no Runtime) a kill is
// simply not propagated to the store.
func (d *Daemon) SetKillHook(fn func(sessID string)) {
	d.hookMu.Lock()
	d.killHook = fn
	d.hookMu.Unlock()
}

// notifyKill invokes the user-kill hook if one is wired. Best-effort.
func (d *Daemon) notifyKill(sessID string) {
	d.hookMu.Lock()
	fn := d.killHook
	d.hookMu.Unlock()
	if fn != nil {
		fn(sessID)
	}
}

// SetTranscriptRepointer registers the callback ingestHook invokes — before it
// remaps a post-/resume hook's divergent live id to the tab id — so the Runtime
// can repoint the tab's transcript tailer at the live <liveId>.jsonl. Until set
// (no Runtime), a resume divergence is simply not repointed.
func (d *Daemon) SetTranscriptRepointer(fn func(tabID, liveID string)) {
	d.hookMu.Lock()
	d.repointHook = fn
	d.hookMu.Unlock()
}

// TopicSignalKind discriminates the turn-cycle signals ingestHook forwards to the
// topic gate in captureLoop.
type TopicSignalKind int

const (
	// TopicPrompt is a UserPromptSubmit: the gate evaluates the opening prompt's
	// correction smell and stashes it on the session state (D1).
	TopicPrompt TopicSignalKind = iota
	// TopicStop is a Stop: the gate drains the tailer, runs, and (if it fires)
	// spawns the judge for the just-finished turn.
	TopicStop
)

// TopicSignal carries a turn-cycle signal for one tab from ingestHook to the
// topic gate. Prompt is the opening prompt text (TopicPrompt only).
type TopicSignal struct {
	Kind   TopicSignalKind
	TabID  string
	Prompt string
}

// SetTopicHook registers the callback ingestHook uses to forward Stop /
// UserPromptSubmit signals into captureLoop's topic gate (U6). Until set (no
// Runtime, or topic-focus disabled), the signals are simply not forwarded and the
// existing status/auto-name behavior is unchanged.
func (d *Daemon) SetTopicHook(fn func(sig TopicSignal)) {
	d.hookMu.Lock()
	d.topicHook = fn
	d.hookMu.Unlock()
}

// forwardTopic forwards a turn-cycle signal to the topic gate if one is wired.
// Best-effort and non-blocking from the caller's view (the hook itself must never
// block a Claude Code turn); the Runtime's hook implementation does the
// non-blocking send onto its own channel.
func (d *Daemon) forwardTopic(sig TopicSignal) {
	d.hookMu.Lock()
	fn := d.topicHook
	d.hookMu.Unlock()
	if fn != nil {
		fn(sig)
	}
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
	// Before remapping, detect a post-/resume id divergence: the hook carries the
	// live transcript id in SessionID and the stable tab id in PinnedSessionID.
	// When they differ, claude has begun writing a NEW transcript at <liveId>.jsonl
	// and the tab's tailer (keyed on the tab id) is now watching a frozen file —
	// so notify the repoint hook with (tabID, liveID) so the Runtime repoints that
	// tailer at the live file. This MUST happen before the remap below overwrites
	// SessionID with the tab id.
	liveID := h.SessionID
	d.hookMu.Lock()
	repoint := d.repointHook
	d.hookMu.Unlock()
	if h.PinnedSessionID != "" && liveID != "" && liveID != h.PinnedSessionID && repoint != nil {
		repoint(h.PinnedSessionID, liveID)
	}
	// Route every hook to the TAB id — the pinned launch session the mux and HQ
	// key on. After an in-session /resume, Claude's live session_id changes to the
	// resumed conversation's id, but the hook shim tags the payload with the
	// original CLAUDE_PLUS_SESSION; without this remap the lookups below would miss
	// and the tab would never auto-name (it would stay on the "session" placeholder)
	// nor update its status. Falls back to the live id when the tag is absent.
	if h.PinnedSessionID != "" {
		h.SessionID = h.PinnedSessionID
	}
	// UserPromptSubmit fires on every prompt the user submits, but ApplyAutoName
	// is idempotent: only the FIRST turn renames the session. A freshly spawned tab
	// that immediately /resume-s an older conversation has not consumed its first
	// turn, so the next prompt typed after the resume is what names it — exactly the
	// "regenerate the slug on the next message turn" behavior. When it renames, emit
	// a session.rename carrying both the derived slug and the (trimmed, capped) raw
	// prompt as the summary, then push a fresh session list so attached CLI clients'
	// sub-tab row updates at once (the emit alone only feeds the Stream panel, not
	// SetSubs). This is the authoritative auto-name path; the transcript tailer's
	// onFirst remains a fallback. We do NOT fall through to MapHook for this kind
	// (UserPromptSubmit carries no status transition).
	if h.HookEventName == "UserPromptSubmit" {
		if renamed, name := d.mux.ApplyAutoName(h.SessionID, h.Prompt); renamed {
			d.emitSession(h.SessionID, event.SessionRenameWithSummary(h.SessionID, name, firstPromptSummary(h.Prompt)))
			d.broadcastSessList()
		}
		// Forward the opening prompt to the topic gate so it can stash a correction
		// smell for this turn's subsequent Stop (D1). Off the critical path.
		d.forwardTopic(TopicSignal{Kind: TopicPrompt, TabID: h.SessionID, Prompt: h.Prompt})
		return
	}
	// Forward a Stop to the topic gate so captureLoop drains the tailer and runs the
	// gate/judge for the just-finished turn (U6). This is in ADDITION to the
	// status.change -> idle mapping below, which is unchanged — the topic gate is a
	// side channel and never delays the idle transition.
	if h.HookEventName == "Stop" {
		d.forwardTopic(TopicSignal{Kind: TopicStop, TabID: h.SessionID})
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

// firstPromptSummary trims a raw first prompt and caps it to a display-friendly
// length so the session.rename summary stays bounded on the wire.
func firstPromptSummary(prompt string) string {
	s := strings.TrimSpace(prompt)
	const max = 200
	if len(s) > max {
		s = s[:max]
	}
	return s
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
	// Register a session-list sink so out-of-attach renames (auto-rename/title)
	// can push this client a fresh sub-tab list. Reuses the same `send` (and its
	// write mutex) as the attach loop, so writes stay serialized per client.
	d.listSinks[clientID] = func() {
		send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
	}
	d.mu.Unlock()

	defer func() {
		d.mux.RemoveSink(clientID)
		d.mux.UnregisterClient(clientID)
		d.RemoveEventSink(clientID)
		d.mu.Lock()
		delete(d.clients, clientID)
		delete(d.listSinks, clientID)
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
		case FrameRename:
			if name, ok := d.mux.Rename(f.SessID, f.Name); ok {
				// Broadcast the rename as an event (Stream + HQ + any other client's
				// session.rename listener), then ack this client with a fresh list so
				// its sub-tab row updates immediately.
				d.emitSession(f.SessID, event.SessionRename(f.SessID, name))
			}
			send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
		case FrameKill:
			// Force-close one session and ack this client with a fresh list so the
			// row disappears immediately. Killing the last session leaves the list
			// empty (no auto-spawn on a later empty transition — only at attach).
			// Notify the Runtime FIRST so it records the user-intent end and drops the
			// session from the cross-restart resume store before the mux removal races
			// the capture loop's left-mux branch.
			d.notifyKill(f.SessID)
			d.mux.Kill(f.SessID)
			send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
		case FrameShutdown:
			// Quit: terminate every session and stop the daemon process, then end
			// this attach loop. d.Stop closes sessions (mux.CloseAll), the listener,
			// and the registry record; it is idempotent.
			d.Stop()
			return
		case FrameSessLs:
			send(Frame{Type: FrameSessAck, List: d.sessInfosFor(clientID)})
		case FramePing:
			send(Frame{Type: FramePong, Version: ProtocolVersion, Sessions: d.mux.Count()})
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
