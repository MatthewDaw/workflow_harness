package daemon

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/workflow-harness/claude-plus/internal/capture"
	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/diag"
	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/judge"
	"github.com/workflow-harness/claude-plus/internal/title"
	"github.com/workflow-harness/claude-plus/internal/topic"
	"github.com/workflow-harness/claude-plus/internal/transport"
)

// Runtime ties the daemon's session mux to the capture and transport layers: it
// tails each session's transcript, maps activity to envelopes, applies auto
// names, and streams envelopes outbound with offline buffering + control
// receive. It is started by the detached daemon process when HQ credentials are
// present; without credentials the daemon still hosts sessions locally.
type Runtime struct {
	d          *Daemon
	client     *transport.Client
	seq        *transport.Seq
	stop       chan struct{}
	instanceID string
	host       string

	// store is the cross-restart resume persistence (Part B): the ordered list of
	// {TabID, Name, naming latches, TranscriptOffset, NextSeq} the daemon reloads on
	// start to `claude --resume <TabID>` each prior session and keep HQ streaming it
	// as the SAME logical session. captureLoop upserts it on first announce, marks it
	// dirty on rename/title, and records (offset, nextSeq) after each advancing Poll;
	// it is flushed synchronously on graceful stop. A session is REMOVED only on a
	// user-intent end (FrameKill / control kill+shutdown) — never on restart/crash, so
	// a resumed conversation survives. nil when no store was provided.
	store *SessionsStore

	// resumeIDs is the set of session ids restored via SpawnResumed on this start. A
	// resumed session's tailer is seeded to the persisted TranscriptOffset (so it
	// never re-emits an already-streamed line) instead of starting at offset 0;
	// resumeOffset carries that per-id starting offset. A FRESH (non-resumed) session
	// keeps the offset-0 default. resumeUsed records which resumed ids have had their
	// offset consumed so a later same-id reuse does not wrongly re-seed.
	resumeIDs     map[string]bool
	resumeOffset  map[string]int64
	resumeStarted map[string]time.Time

	// userKilledMu guards userKilled, the set of session ids a user (or HQ control)
	// intentionally ended. captureLoop's left-the-mux branch removes such ids from the
	// resume store; a session that left the mux for any OTHER reason (notably a daemon
	// restart, which never runs this loop) is preserved for continuity.
	userKilledMu sync.Mutex
	userKilled   map[string]bool

	// cfgSrc is HQ's effective (org+user+project resolved) skills/agents source,
	// set when an HQ REST base is configured. Used both for the drift meter and
	// the per-session skills auto-sync (#1). nil when HQ REST is unconfigured.
	cfgSrc config.RemoteSource

	// syncedMu guards syncedSessions, which records which sessions have already
	// triggered the one-shot skills auto-sync (so it runs once per NEW session).
	syncedMu       sync.Mutex
	syncedSessions map[string]bool

	// termMu guards terminated, the set of sessions that have been force-shut-down
	// (HQ shutdown/kill) or have otherwise left the mux. emit consults it to
	// suppress any late revival event (a straggling 'active'/message/heartbeat from
	// a survivor or an in-flight hook), so a force-shut row cannot bounce back to
	// life. The terminal 'done' event itself is always allowed through.
	termMu     sync.Mutex
	terminated map[string]bool

	// repointCh carries (tabID, liveID) repoint requests from the daemon's
	// ingestHook (a post-/resume id divergence) to captureLoop, which owns the
	// per-tab tailers. Buffered + non-blocking send so a hook never blocks on a
	// busy capture loop.
	repointCh chan [2]string

	// topicCh carries turn-cycle signals (Stop / UserPromptSubmit) from ingestHook
	// to captureLoop, which owns the topic-focus gate + judge (U6). Buffered +
	// non-blocking send so a hook never blocks; a dropped signal is harmless (the
	// cadence floor re-fires the gate on the next turn).
	topicCh chan daemonTopicSignal

	// judgeLimit is the daemon-wide concurrency cap on judge spawns shared across
	// every tab (D3): a token bucket of 2 concurrent headless model calls.
	judgeLimit *topic.Limiter
	// topicStore checkpoints per-session topic state under the claude+ config dir,
	// keyed by tab session id (C6). nil when the config dir is unavailable, in which
	// case the gate runs on ephemeral in-memory state.
	topicStore *topic.Store
}

// daemonTopicSignal mirrors daemon.TopicSignal locally so captureLoop owns a copy
// it can select on without importing across the hook boundary back into itself.
type daemonTopicSignal struct {
	stop   bool // true = Stop; false = UserPromptSubmit
	tabID  string
	prompt string
}

// topicEntry carries a tab's topic state behind a mutex. The gate's synchronous
// mutations (Gate, LastOffset) run on the capture loop while a prior turn's judge
// goroutine may still be folding its verdict into the SAME state; the mutex makes
// those two writers safe (the judge call itself runs OUTSIDE the lock so a slow
// model never blocks the loop — only the cheap Fold + emit are serialized).
type topicEntry struct {
	mu sync.Mutex
	st *topic.State
}

// runJudge is the seam captureLoop calls to classify a turn. It defaults to the
// real headless judge but is a package var so the U6 wiring tests can inject a
// deterministic stub without spawning a model (mirrors the title/judge runClaude
// seam). It returns the parsed verdict in the topic package's local shape.
var runJudge = func(in judge.Input) (topic.Verdict, bool) {
	v, failed := judge.Judge(in)
	return topic.Verdict{
		SameTopic:      v.SameTopic,
		TopicLabel:     v.TopicLabel,
		Description:    v.Description,
		IsCorrection:   v.IsCorrection,
		ContradictsDoc: v.ContradictsDoc,
		ImplLearning:   v.ImplLearning,
		DocQuestion:    v.DocQuestion,
	}, failed
}

// emit wraps a captured event in an envelope and fans it to local subscribers
// (the Stream panel) plus HQ when configured. It is the single send path shared
// by the transcript tailer and the hook receiver (U18), so hook-sourced and
// tailer-sourced events are sequenced and delivered identically.
func (rt *Runtime) emit(sid string, e event.Event) {
	// Tombstone gate: once a session is terminated, suppress every later event for
	// it EXCEPT its own terminal status.change -> done. This stops a survivor's
	// straggling 'active'/message or a heartbeat — or a late in-flight hook — from
	// reviving a force-shut-down row (BUG 1). The done event must still pass so HQ
	// retires the live row.
	isDone := e.Kind == event.KindStatusChange && e.To == event.StatusDone
	if !isDone && rt.isTerminated(sid) {
		return
	}
	env := event.Envelope{
		V: 1, InstanceID: rt.instanceID, Host: rt.host,
		TS: time.Now().UnixMilli(), Seq: rt.seq.Next(sid), Event: e,
	}
	rt.d.PublishEvent(env) // local subscribers (Stream panel)
	if rt.client != nil {
		_ = rt.client.Send(env) // HQ, when configured
	}
}

// markTerminated tombstones a session id so emit suppresses any subsequent
// (non-done) event for it. Called at every teardown site BEFORE the terminal
// done is emitted (recv.Terminated and the captureLoop left-the-mux branch).
func (rt *Runtime) markTerminated(id string) {
	rt.termMu.Lock()
	if rt.terminated == nil {
		rt.terminated = map[string]bool{}
	}
	rt.terminated[id] = true
	rt.termMu.Unlock()
}

// isTerminated reports whether a session has been tombstoned.
func (rt *Runtime) isTerminated(id string) bool {
	rt.termMu.Lock()
	defer rt.termMu.Unlock()
	return rt.terminated[id]
}

// markUserKilled records that a session was intentionally ended by the user
// (FrameKill) or HQ control (kill/shutdown via recv.Terminated). captureLoop's
// left-the-mux branch consults this to decide whether to REMOVE the session from
// the cross-restart resume store: a user-intent end removes it (so it is not
// --resume-d on the next start); any other departure preserves it for continuity.
func (rt *Runtime) markUserKilled(id string) {
	rt.userKilledMu.Lock()
	if rt.userKilled == nil {
		rt.userKilled = map[string]bool{}
	}
	rt.userKilled[id] = true
	rt.userKilledMu.Unlock()
}

// isUserKilled reports whether a session was intentionally ended by user/control.
func (rt *Runtime) isUserKilled(id string) bool {
	rt.userKilledMu.Lock()
	defer rt.userKilledMu.Unlock()
	return rt.userKilled[id]
}

// removeFromStore deletes a session from the resume store and marks it dirty
// (best-effort, nil-store safe). Called only on a user-intent end so a
// deliberately-killed session is not resumed on the next daemon start.
func (rt *Runtime) removeFromStore(id string) {
	if rt.store != nil {
		rt.store.Remove(id)
	}
}

// reconcileResumeSeqs seeds the per-session seq floor so a resumed session's NEW
// envelopes carry seqs strictly greater than (a) the persisted NextSeq from the
// store AND (b) 1 + the max seq of any still-unacked envelope in the ring buffer
// (those replay with their ORIGINAL seqs on reconnect). Without this a resumed
// session could re-use a seq HQ already folded (or one about to be replayed),
// which HQ dedupes/drops as stale — the exact correctness failure Part B guards.
// It is safe to call with a nil buffer (then only the store floor applies) and a
// nil store (then only the buffer floor applies).
func (rt *Runtime) reconcileResumeSeqs(buf *transport.RingBuffer) {
	// Max seq per session still pending (un-acked) in the ring buffer.
	maxBuffered := map[string]int64{}
	if buf != nil {
		if pending, err := buf.Pending(); err == nil {
			for _, env := range pending {
				sid := env.Event.SessionID
				if env.Seq > maxBuffered[sid] {
					maxBuffered[sid] = env.Seq
				}
			}
		}
	}
	// Seed the floor for every persisted session and for any session that has a
	// buffered envelope (even one not in the store, e.g. a ghost), so Next() can
	// only ever produce values beyond anything previously delivered or buffered.
	floors := map[string]int64{}
	if rt.store != nil {
		for _, ps := range rt.store.Sessions() {
			if ps.NextSeq > floors[ps.TabID] {
				floors[ps.TabID] = ps.NextSeq
			}
		}
	}
	for sid, m := range maxBuffered {
		if m+1 > floors[sid] {
			floors[sid] = m + 1
		}
	}
	for sid, n := range floors {
		rt.seq.SeedFloor(sid, n)
	}
}

// upsertSession writes the session's current naming state into the resume store,
// preserving any already-persisted TranscriptOffset/NextSeq for the id (so a
// first-announce upsert does not clobber a resumed session's offset/seq). It
// marks the store dirty (debounced flush). nil-store safe.
func (rt *Runtime) upsertSession(id string) {
	if rt.store == nil {
		return
	}
	s := rt.d.mux.Get(id)
	if s == nil {
		return
	}
	name, firstSet, titleSet, manualName := s.NamingState()
	// Preserve existing offset/seq for the id if present (e.g. a resumed session).
	var off, nextSeq int64
	for _, ps := range rt.store.Sessions() {
		if ps.TabID == id {
			off, nextSeq = ps.TranscriptOffset, ps.NextSeq
			break
		}
	}
	rt.store.Upsert(PersistedSession{
		TabID:            id,
		Name:             name,
		ManualName:       manualName,
		FirstSet:         firstSet,
		TitleSet:         titleSet,
		TranscriptOffset: off,
		NextSeq:          nextSeq,
	})
}

// recordOffset persists a tailer's advanced byte offset together with the
// session's current next seq (Seq.Peek, which reads without consuming) as ONE
// unit — so TranscriptOffset and NextSeq always describe the same moment — plus
// the latest naming state. It only writes (and marks dirty) when the offset or
// seq actually advanced, so an idle session does not churn the store. The
// debounced flusher coalesces the dirty marks into at most one Save per ~2s.
func (rt *Runtime) recordOffset(id string, off int64) {
	if rt.store == nil {
		return
	}
	// Find the existing entry to detect an advance and to preserve naming if the
	// session has since left the mux.
	var prev *PersistedSession
	for _, ps := range rt.store.Sessions() {
		if ps.TabID == id {
			cp := ps
			prev = &cp
			break
		}
	}
	nextSeq := rt.seq.Peek(id)
	if prev != nil && prev.TranscriptOffset == off && prev.NextSeq == nextSeq {
		return // nothing advanced — do not churn the store
	}
	name, firstSet, titleSet, manualName := prev.naming()
	if s := rt.d.mux.Get(id); s != nil {
		name, firstSet, titleSet, manualName = s.NamingState()
	}
	rt.store.Upsert(PersistedSession{
		TabID:            id,
		Name:             name,
		ManualName:       manualName,
		FirstSet:         firstSet,
		TitleSet:         titleSet,
		TranscriptOffset: off,
		NextSeq:          nextSeq,
	})
}

// checkResumeFailures implements the Part B resume-failure fallback: if a
// session restored via SpawnResumed exits within resumeFailWindow of spawn, the
// `claude --resume <id>` child failed to reattach (e.g. the conversation no
// longer exists). Treat it as a dead resume: tombstone + emit done for the old
// id, drop it from the store, and spawn a FRESH session in its place — sequenced
// done-before-start so HQ retires the old row before the new one appears.
func (rt *Runtime) checkResumeFailures() {
	if rt.store == nil || len(rt.resumeStarted) == 0 {
		return
	}
	now := time.Now()
	for id, started := range rt.resumeStarted {
		s := rt.d.mux.Get(id)
		alive := s != nil && s.Alive()
		if alive {
			// Survived the window: it is a healthy resume; stop watching it.
			if now.Sub(started) >= resumeFailWindow {
				delete(rt.resumeStarted, id)
			}
			continue
		}
		// Child is gone (or never registered). Only treat an exit WITHIN the window as
		// a resume FAILURE; an exit after the window is a normal end handled by the
		// left-mux branch.
		if now.Sub(started) >= resumeFailWindow {
			delete(rt.resumeStarted, id)
			continue
		}
		delete(rt.resumeStarted, id)
		// done-before-start: retire the failed-resume row, then spawn fresh.
		rt.markTerminated(id)
		rt.emit(id, event.StatusChange(id, event.StatusActive, event.StatusDone))
		rt.removeFromStore(id)
		if _, err := rt.d.mux.Spawn(""); err != nil {
			diag.Logf("resume fallback: spawn fresh after failed resume of %s: %v", id, err)
		}
	}
}

// naming reads the naming fields off a PersistedSession (nil-safe), so
// recordOffset can preserve them when the live session is already gone.
func (ps *PersistedSession) naming() (name string, firstSet, titleSet, manualName bool) {
	if ps == nil {
		return "", false, false, false
	}
	return ps.Name, ps.FirstSet, ps.TitleSet, ps.ManualName
}

// resumeFailWindow bounds how soon after SpawnResumed a child exit counts as a
// failed resume (vs. a normal later end). A `claude --resume` against a missing
// conversation fails fast; a healthy resume keeps running well past this.
const resumeFailWindow = 3 * time.Second

// heartbeatInterval is how often the runtime emits a session.heartbeat for each
// live session. Comfortably below the backend's 60s stale window so a genuinely
// alive idle session always refreshes before it would be considered stale.
const heartbeatInterval = 20 * time.Second

// heartbeatLoop emits a session.heartbeat for every live session on a fixed
// interval until the daemon shuts down. Best-effort and non-blocking: it reuses
// the shared emit path (local bus + HQ when configured) and never blocks the
// daemon — a dead session list simply produces no heartbeats. When the daemon
// process dies (e.g. power loss) the loop stops with it, so HQ stops seeing
// heartbeats and read-time freshness retires those sessions.
func (rt *Runtime) heartbeatLoop() {
	defer diag.Recover("runtime.heartbeatLoop")
	tk := time.NewTicker(heartbeatInterval)
	defer tk.Stop()
	for {
		select {
		case <-rt.stop:
			return
		case <-tk.C:
			for _, v := range rt.d.mux.List() {
				// Liveness gate: a dead/zombie child can linger in mux.List() on
				// Windows ConPTY (no PTY EOF to drive removal), so heartbeating every
				// listed id would keep a corpse looking "live". Skip any session whose
				// child has actually exited or been closed (BUG 2).
				s := rt.d.mux.Get(v.ID)
				if s == nil || !s.Alive() {
					continue
				}
				rt.emit(v.ID, event.SessionHeartbeat(v.ID))
			}
		}
	}
}

// hqConfig is read from ~/.claude-plus/credentials (written by `claude+ login`).
type hqConfig struct {
	URL   string
	Token string
}

// loadHQConfig reads HQ connection details, or returns ok=false if unconfigured.
func loadHQConfig() (hqConfig, bool) {
	url := os.Getenv("CLAUDE_PLUS_WS_URL")
	token := os.Getenv("CLAUDE_PLUS_TOKEN")
	if url != "" && token != "" {
		return hqConfig{URL: url, Token: token}, true
	}
	// Fall back to the credentials file (best-effort; format kept simple).
	home, err := os.UserHomeDir()
	if err != nil {
		return hqConfig{}, false
	}
	b, err := os.ReadFile(filepath.Join(home, ".claude-plus", "credentials"))
	if err != nil {
		return hqConfig{}, false
	}
	// credentials file is "url\ntoken\n".
	lines := splitLines(string(b))
	if len(lines) >= 2 && lines[0] != "" && lines[1] != "" {
		return hqConfig{URL: lines[0], Token: lines[1]}, true
	}
	return hqConfig{}, false
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, trimCR(s[start:i]))
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, trimCR(s[start:]))
	}
	return out
}

func trimCR(s string) string {
	if len(s) > 0 && s[len(s)-1] == '\r' {
		return s[:len(s)-1]
	}
	return s
}

// StartRuntime starts the capture loop for a daemon and, when HQ credentials are
// present, also streams envelopes outbound. Capture runs regardless of
// credentials so local subscribers (the desktop Stream panel via the daemon
// event bus) see events even without `claude+ login`; only the outbound HQ
// transport is gated on credentials. The returned Runtime is stopped on daemon
// shutdown.
func StartRuntime(d *Daemon, instanceID string) *Runtime {
	return StartRuntimeWithStore(d, instanceID, nil)
}

// StartRuntimeWithStore is StartRuntime plus the Part B cross-restart resume
// store. When store is non-nil the runtime: (1) seeds the per-session seq floor
// from the persisted NextSeq and the ring buffer's max un-acked seq BEFORE any
// event is emitted (so a resumed session's new seqs strictly exceed everything HQ
// already saw or will replay); (2) seeds each resumed session's tailer offset
// from the persisted TranscriptOffset (so it never re-emits an already-streamed
// line); (3) upserts the store on first announce, marks it dirty on rename/title
// and after each advancing Poll, and flushes it synchronously on graceful stop;
// (4) removes a session from the store only on a user-intent end. The caller
// (RunDaemon) is expected to have already SpawnResumed + SeedNaming each persisted
// session against d.Mux() so they are live in the mux when captureLoop announces
// them. nil store reproduces the exact pre-Part-B StartRuntime behavior.
func StartRuntimeWithStore(d *Daemon, instanceID string, store *SessionsStore) *Runtime {
	rt := &Runtime{d: d, seq: transport.NewSeq(), stop: make(chan struct{}),
		instanceID: instanceID, host: hostName(),
		store:          store,
		syncedSessions: map[string]bool{},
		terminated:     map[string]bool{},
		userKilled:     map[string]bool{},
		resumeIDs:      map[string]bool{},
		resumeOffset:   map[string]int64{},
		resumeStarted:  map[string]time.Time{},
		repointCh:      make(chan [2]string, 16),
		topicCh:        make(chan daemonTopicSignal, 64),
		judgeLimit:     topic.NewLimiter(2)}

	// Seed resume metadata from the store: which ids are resumed (so their tailer
	// starts at the persisted offset, not 0) and a watcher start time per id (so the
	// resume-failure fallback can tell an early child exit from a normal later exit).
	if store != nil {
		now := time.Now()
		for _, ps := range store.Sessions() {
			rt.resumeIDs[ps.TabID] = true
			rt.resumeOffset[ps.TabID] = ps.TranscriptOffset
			rt.resumeStarted[ps.TabID] = now
		}
	}

	// Checkpoint store for per-session topic state, under the claude+ config dir
	// (C6). Best-effort: an unavailable config dir leaves topicStore nil and the
	// gate runs on ephemeral state.
	if dir, err := config.EnsureConfigDir(d.repoRoot); err == nil {
		if st, err := topic.NewStore(dir); err == nil {
			rt.topicStore = st
		}
	}

	// Route hook-shim events through the same emit path as the tailer (U18).
	d.SetHookIngestor(rt.emit)

	// Forward turn-cycle signals (Stop / UserPromptSubmit) into captureLoop's topic
	// gate (U6). Non-blocking send so a hook never blocks on a busy loop; a dropped
	// signal is harmless (the cadence floor re-fires the gate on the next turn).
	d.SetTopicHook(func(sig TopicSignal) {
		ds := daemonTopicSignal{stop: sig.Kind == TopicStop, tabID: sig.TabID, prompt: sig.Prompt}
		select {
		case rt.topicCh <- ds:
		default:
		}
	})

	// Repoint the tab's transcript tailer when a post-/resume hook reveals a
	// diverged live id (BUG 2). ingestHook fires this BEFORE remapping the id; we
	// hand the request off to captureLoop (which owns the tailers) via a
	// non-blocking send so a hook never blocks on a busy loop — a dropped request
	// is harmless, the next hook re-requests the same repoint.
	d.SetTranscriptRepointer(func(tab, live string) {
		select {
		case rt.repointCh <- [2]string{tab, live}:
		default:
		}
	})

	// Install the managed hooks block in this repo's per-project config root's
	// settings.json so Claude Code forwards lifecycle events to `claude+ __hook`
	// (which delivers them to this daemon's socket). Best-effort: a missing
	// executable path or unwritable settings file must never block daemon startup,
	// so errors are ignored.
	installHooks(d.repoRoot)

	// Register the user-kill hook so an attached client's FrameKill records the
	// user-intent end (and removes the session from the resume store). The control
	// path (recv.Terminated) does the same below.
	d.SetKillHook(func(sessID string) {
		rt.markUserKilled(sessID)
		rt.removeFromStore(sessID)
	})

	if cfg, ok := loadHQConfig(); ok {
		home, _ := os.UserHomeDir()
		if buf, err := transport.OpenRingBuffer(filepath.Join(home, ".claude-plus", "outbound.jsonl")); err == nil {
			// Part B seq reconciliation: BEFORE the client replays the buffer (which
			// re-sends un-acked envelopes with their ORIGINAL seqs) and BEFORE the
			// capture loop emits anything, raise each resumed session's seq floor above
			// both the persisted NextSeq and 1 + the max buffered seq, so new envelopes
			// strictly exceed everything HQ has seen or will replay.
			rt.reconcileResumeSeqs(buf)
			recv := d.NewControlReceiver()
			// When HQ force-shuts-down a session (or a ghost from a dead daemon),
			// emit a terminal status.change -> done so HQ drops it from the live
			// list and the row the user clicked actually disappears (#1, #2).
			recv.Terminated = func(sessionID string) {
				// A force shutdown/kill is a user-intent end: record it and drop the
				// session from the resume store so the next daemon start does NOT
				// --resume a conversation HQ deliberately terminated.
				rt.markUserKilled(sessionID)
				rt.removeFromStore(sessionID)
				// Tombstone BEFORE emitting done: any later straggler event for this id
				// (a survivor's 'active', a late hook) is then suppressed by emit, so a
				// force-shut row cannot bounce back to life (BUG 1). The done itself is
				// exempt from suppression.
				rt.markTerminated(sessionID)
				rt.emit(sessionID, event.StatusChange(sessionID, event.StatusActive, event.StatusDone))
			}
			rt.client = transport.NewClient(cfg.URL, cfg.Token, instanceID, buf, recv.Handle)
			go rt.client.Run()
		}
	} else {
		// No HQ client (local-only daemon): there is no ring buffer to scan, but the
		// store's persisted NextSeq must still floor the seq generator so a resumed
		// session never re-uses a seq for local subscribers either.
		rt.reconcileResumeSeqs(nil)
	}

	// Per-session transcript tailers feed the envelope stream (local bus + HQ).
	go rt.captureLoop(instanceID)

	// Heartbeat: periodically emit session.heartbeat for each live session so HQ's
	// lastEventAt stays fresh even while a session is idle. If the laptop loses
	// power the daemon dies and stops heartbeating, so HQ's read-time freshness
	// drops those sessions within the stale window (no reaper needed).
	go rt.heartbeatLoop()

	// Agents/skills drift meter (U19): poll HQ's effective registry and fold the
	// drift count into the status snapshot. Only runs when an HQ REST base is
	// configured; otherwise the meter stays at 0 (no remote to compare against).
	if base, ok := loadAPIBase(); ok {
		if cfg, ok := loadHQConfig(); ok {
			src := config.NewHTTPRemoteSource(base, cfg.Token, projectIDFor(d.repoRoot))
			rt.cfgSrc = src
			// Auto-sync skills once at startup so applicable (org+user+project)
			// skills are present before the first session even announces (#1).
			go rt.reconcileSkills()
			go rt.configSyncLoop(src)
		}
	}
	return rt
}

// reconcileSkills pulls HQ's effective skills/agents that are missing locally and
// pushes local-only ones, so skills matching the user's applicable scopes appear
// in ~/.claude. It is best-effort: every error is logged and swallowed so it can
// never block a session (#1). Called once at startup and once per new session.
func (rt *Runtime) reconcileSkills() {
	defer diag.Recover("runtime.reconcileSkills")
	src := rt.cfgSrc
	if src == nil {
		return
	}
	// EnsureConfigDir (not the pure ProjectConfigDir) so the root + its seed-once
	// .mcp.json (the user's personal MCP servers) exist BEFORE a pull can merge HQ
	// servers into a fresh .mcp.json — otherwise a sync-before-first-spawn would
	// create .mcp.json with only HQ servers and the seed would later be skipped.
	plus, err := config.EnsureConfigDir(rt.d.repoRoot)
	if err != nil {
		diag.Logf("skills auto-sync: resolve project config dir failed: %v", err)
		return
	}
	report, err := config.ComputeDrift(src, plus)
	if err != nil {
		diag.Logf("skills auto-sync: compute drift failed: %v", err)
		return
	}
	local, err := config.ReadLocal(plus)
	if err != nil {
		diag.Logf("skills auto-sync: read local failed: %v", err)
		return
	}
	pulled, pushed, errs := config.Reconcile(report, src, local, plus)
	for _, e := range errs {
		diag.Logf("skills auto-sync: %v", e)
	}
	if pulled > 0 || pushed > 0 {
		diag.Logf("skills auto-sync: pulled %d, pushed %d", pulled, pushed)
	}
	// Refresh the drift meter to reflect the post-reconcile state.
	_ = rt.d.SyncConfigOnce(src)
}

// SyncSkillsNow performs a one-shot skills/agents reconcile against HQ for the
// given repo: HQ-only items are materialized into ~/.claude+ and local-only ones
// pushed to HQ user scope (U19/U21). It is the on-demand entrypoint behind the
// `/update-skills` skill and `claude+ sync-skills`, reusing the same source +
// reconcile the daemon runs automatically per session. After reconcile it runs the
// verification gate (U-Verify-Gate) over the effective enabled set: a partial
// install (a missing/invalid skill, agent, or failed MCP server) returns a non-nil
// error so the caller fails loudly. The gate report is returned too so the caller
// can surface needs-auth MCP servers (which do NOT fail the gate). Returns counts
// actuated plus the gate report.
func SyncSkillsNow(repoRoot string) (pulled, pushed int, gate config.VerifyReport, err error) {
	base, ok := loadAPIBase()
	if !ok {
		return 0, 0, config.VerifyReport{}, fmt.Errorf("no HQ API base configured (run `claude+ login`)")
	}
	cfg, ok := loadHQConfig()
	if !ok {
		return 0, 0, config.VerifyReport{}, fmt.Errorf("not signed in to HQ (run `claude+ login`)")
	}
	src := config.NewHTTPRemoteSource(base, cfg.Token, projectIDFor(repoRoot))
	// EnsureConfigDir (not ProjectConfigDir): seed the root's personal .claude.json
	// before any pull merges HQ MCP servers into it (see reconcileSkills).
	plus, err := config.EnsureConfigDir(repoRoot)
	if err != nil {
		return 0, 0, config.VerifyReport{}, err
	}
	report, err := config.ComputeDrift(src, plus)
	if err != nil {
		return 0, 0, config.VerifyReport{}, err
	}
	local, err := config.ReadLocal(plus)
	if err != nil {
		return 0, 0, config.VerifyReport{}, err
	}
	pulled, pushed, errs := config.Reconcile(report, src, local, plus)
	// Reconcile errors are NON-FATAL here. A PUSH failure must not abort a
	// pull-focused sync: publishing a local-only skill/agent to the org catalog is
	// admin-gated (POST /skills|/agents), so a non-admin device token legitimately
	// gets 401 — that is expected and is not a sync failure. A failed PULL is not
	// swallowed: the item simply won't be on disk, so the verify gate below catches
	// it. Log the reconcile errors; the gate is the arbiter of success.
	for _, e := range errs {
		diag.Logf("sync-skills: non-fatal reconcile error (e.g. cannot publish without admin): %v", e)
	}
	// U-Verify-Gate: after reconcile, verify the EFFECTIVE enabled set actually
	// landed on disk (skill dirs + frontmatter; agent files + deps; MCP not failed).
	// A partial install fails loudly (non-zero) rather than reporting a misleading
	// "pulled N". needs-auth MCP servers do NOT fail the gate.
	gate, gErr := config.VerifyEffectiveSet(src, plus)
	if gErr != nil {
		return pulled, pushed, config.VerifyReport{}, gErr
	}
	if err := gate.Err(); err != nil {
		return pulled, pushed, gate, err
	}
	return pulled, pushed, gate, nil
}

// SyncMemoriesNow performs a one-shot FULL RECONCILE of the local Claude Code
// project "memories" up to HQ for the given repo: it reads every memory/<slug>.md
// under this project's isolated config dir and PUTs the whole set to
// /projects/{id}/memories, which replaces the caller's entire set server-side
// (adds, updates, AND deletions on disk all propagate; an empty set clears it).
// It mirrors SyncSkillsNow's config resolution (API base + device token the same
// way) and projectIDFor(repoRoot). A missing base/token is a no-op (return nil),
// NOT an error: a daemon running without `claude+ login` simply doesn't sync —
// this is the end-of-turn reconcile and must never destabilize an unauthenticated
// session. ReadLocalMemories' error is tolerated (a missing memory/ dir yields an
// empty set, which is a valid reconcile), so the only hard error is the PUT.
func SyncMemoriesNow(repoRoot string) error {
	base, ok := loadAPIBase()
	if !ok {
		return nil // no HQ API base configured — nothing to sync to (no-op)
	}
	cfg, ok := loadHQConfig()
	if !ok || cfg.Token == "" {
		return nil // not signed in — nothing to sync (no-op)
	}
	memoryDir, err := capture.MemoryDir(repoRoot)
	if err != nil {
		return err
	}
	// A missing dir / read hiccup yields an empty set; reconcile it anyway (an empty
	// PUT clears the author's set, which is the correct full-reconcile semantics).
	items, _ := config.ReadLocalMemories(memoryDir)
	return config.ReconcileMemories(base, cfg.Token, projectIDFor(repoRoot), items)
}

// syncSkillsOnce triggers the skills auto-sync the first time a given session is
// observed. Subsequent observations of the same session are no-ops, so the sync
// runs once per NEW session (#1). The reconcile runs on its own goroutine so it
// never delays the capture loop or the session.
func (rt *Runtime) syncSkillsOnce(sessID string) {
	if rt.cfgSrc == nil {
		return
	}
	rt.syncedMu.Lock()
	if rt.syncedSessions[sessID] {
		rt.syncedMu.Unlock()
		return
	}
	rt.syncedSessions[sessID] = true
	rt.syncedMu.Unlock()
	go rt.reconcileSkills()
}

// forgetSkillSync drops a session's sync marker when it ends, so a reused id
// re-syncs on its next appearance and the map doesn't grow without bound (#11).
func (rt *Runtime) forgetSkillSync(sessID string) {
	rt.syncedMu.Lock()
	delete(rt.syncedSessions, sessID)
	rt.syncedMu.Unlock()
}

// configSyncLoop refreshes the drift meter on a slow tick (agents/skills change
// rarely, and the fetch is a network round-trip). Fetch errors are swallowed so
// a transient HQ blip never crashes the daemon or blanks the meter.
func (rt *Runtime) configSyncLoop(src config.RemoteSource) {
	tk := time.NewTicker(30 * time.Second)
	defer tk.Stop()
	// Each sync runs behind a recover: a panic in the fetch/parse is logged and the
	// bad tick skipped rather than tearing down this poller (which has no other
	// recover) and, with it, the daemon. The loop keeps refreshing the drift meter.
	sync := func() {
		defer diag.Recover("runtime.configSyncLoop")
		_ = rt.d.SyncConfigOnce(src)
	}
	sync() // prime immediately on start
	for {
		select {
		case <-rt.stop:
			return
		case <-tk.C:
			sync()
		}
	}
}

// loadAPIBase resolves HQ's REST base URL (distinct from the WebSocket URL):
// the CLAUDE_PLUS_API_URL env var, or the third line of the credentials file.
func loadAPIBase() (string, bool) {
	if base := os.Getenv("CLAUDE_PLUS_API_URL"); base != "" {
		return base, true
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", false
	}
	b, err := os.ReadFile(filepath.Join(home, ".claude-plus", "credentials"))
	if err != nil {
		return "", false
	}
	lines := splitLines(string(b))
	if len(lines) >= 3 && lines[2] != "" {
		return lines[2], true
	}
	return "", false
}

// captureLoop announces each session to HQ (session.start) and tails its
// transcript, forwarding events outbound. Sessions appearing later are picked up
// on the poll tick. Announcement is independent of the transcript tailer so a
// session shows in HQ immediately, even before claude writes any transcript.
func (rt *Runtime) captureLoop(instanceID string) {
	// tailStops holds a per-session stop channel so a tailer goroutine can be torn
	// down when its session ends (leak #11) — independent of the daemon-wide
	// rt.stop. announced records which sessions have been announced to HQ.
	tailStops := map[string]chan struct{}{}
	announced := map[string]bool{}
	// tailers holds each tab's live Tailer so a post-/resume repoint request can
	// switch its watched file. transcriptID records the file id each tab is
	// currently tailing (initialized to the tab id); a repoint only fires on a
	// genuine divergence (live id != the currently-watched id).
	tailers := map[string]*capture.Tailer{}
	transcriptID := map[string]string{}
	// topicState holds each tab's carried topic-focus state (label, rolling
	// description, cadence/debounce counters, checkpoint offset) behind a per-tab
	// mutex. It is loaded from the checkpoint store the first time a tab is seen (so
	// a daemon restart resumes mid-session) and re-checkpointed after each fold (C6).
	topicState := map[string]*topicEntry{}
	tk := time.NewTicker(time.Second)
	defer tk.Stop()
	host := rt.host
	projectID := projectIDFor(rt.d.repoRoot)
	// repoName is the human-readable repo display name (git "owner/repo" or the
	// repo folder name). Computed once per capture loop and carried on every
	// session.start so HQ can register the Project under a meaningful name.
	repoName := repoNameFor(rt.d.repoRoot)
	emit := rt.emit

	// closeAllTails stops every per-session tailer (daemon shutdown).
	closeAllTails := func() {
		for _, ch := range tailStops {
			close(ch)
		}
	}

	// tick handles exactly one loop iteration behind a recover so a panic in any
	// branch (a malformed transcript row, a topic-gate edge case, a tailer repoint)
	// is logged and the single bad iteration dropped — the loop, its per-session
	// maps, and the whole daemon survive. The detached daemon has no visible stderr,
	// so without this an unrecovered panic here would kill the daemon silently and
	// the user's attached session would just vanish. Returns true only on clean stop.
	tick := func() (done bool) {
		defer diag.Recover("runtime.captureLoop")
		select {
		case <-rt.stop:
			// Part B graceful stop: flush the resume store synchronously so the final
			// (offset, NextSeq, name) of every live session is durable BEFORE the
			// process exits — a daemon restart then resumes from exactly here. This is
			// a graceful stop, NOT a user-intent end, so sessions are PRESERVED in the
			// store (not removed): they will be --resume-d on the next start.
			if rt.store != nil {
				_ = rt.store.FlushNow()
			}
			closeAllTails()
			return true
		case req := <-rt.repointCh:
			// A post-/resume id divergence: repoint tab req[0]'s tailer at the live
			// transcript req[1] so streaming continues from the new file. Only act on
			// a genuine divergence (we are not already tailing that id) and only when
			// the tab actually has a running tailer.
			tab, live := req[0], req[1]
			if t, ok := tailers[tab]; ok && transcriptID[tab] != live {
				if path, err := capture.TranscriptPath(rt.d.repoRoot, live); err == nil {
					t.Repoint(path)
					transcriptID[tab] = live
				}
			}
		case sig := <-rt.topicCh:
			// Topic-focus turn-cycle signal (U6). Owned here in captureLoop because the
			// gate needs the per-tab tailer + transcript path + emit closure that live
			// in this loop. Lazily load the tab's checkpoint on first sight.
			te := topicState[sig.tabID]
			if te == nil {
				te = &topicEntry{st: rt.loadTopicState(sig.tabID)}
				topicState[sig.tabID] = te
			}
			if !sig.stop {
				// UserPromptSubmit: stash the correction smell for this turn's Stop (D1).
				te.mu.Lock()
				if topic.SmellsLikeCorrection(sig.prompt) {
					te.st.MarkCorrection()
				}
				snap := *te.st
				te.mu.Unlock()
				rt.checkpointTopic(sig.tabID, &snap)
				return false
			}
			// Stop: drain the tailer so the turn's tool_use rows are flushed, then run
			// the gate and (if it fires) spawn the judge on a goroutine. The existing
			// status.change -> idle (emitted by ingestHook's MapHook path) is untouched.
			if t, ok := tailers[sig.tabID]; ok {
				_ = t.Poll()
			}
			rt.runTopicGate(sig.tabID, te, emit)
		case <-tk.C:
			live := map[string]bool{}
			for _, v := range rt.d.mux.List() {
				live[v.ID] = true
				// Announce a newly-seen session with session.start (seq 0 for this
				// session) so HQ has its identity — project, name — from the
				// first event, before any transcript activity.
				if !announced[v.ID] {
					announced[v.ID] = true
					emit(v.ID, event.SessionStart(v.ID, projectID, host, v.Name, "", repoName))
					// Part B: upsert the resume store on first announce so a brand-new
					// session is persisted for cross-restart resume immediately, even
					// before it writes any transcript. A resumed session is already in
					// the store; Upsert is replace-by-id, so this just refreshes its
					// name/latches and is harmless.
					rt.upsertSession(v.ID)
				}
				// Auto-sync HQ's effective skills/agents for this user+project on
				// each NEW session so freshly-scoped skills appear locally (best
				// effort; never blocks the session). Runs once per session.
				rt.syncSkillsOnce(v.ID)
				if _, ok := tailStops[v.ID]; ok {
					continue
				}
				path, err := capture.TranscriptPath(rt.d.repoRoot, v.ID)
				if err != nil {
					continue
				}
				sid := v.ID
				onFirst := func(sessID, text string) {
					if renamed, name := rt.d.mux.ApplyAutoName(sessID, text); renamed {
						emit(sessID, event.SessionRename(sessID, name))
						// Push the new name to attached CLI clients' tab strip at once.
						rt.d.broadcastSessList()
					}
				}
				// After the first full exchange, upgrade the provisional slug to a
				// concise LLM-generated title. The headless `claude -p` call is slow
				// and best-effort, so it runs on its own goroutine and never blocks
				// the capture loop; a failure leaves the provisional name in place.
				onExchange := func(sessID, userText, assistantText string) {
					go func() {
						defer diag.Recover("runtime.titleGen")
						raw, ok := title.Generate(rt.d.repoRoot, userText, assistantText)
						if !ok {
							return
						}
						if renamed, name := rt.d.mux.ApplyTitle(sessID, raw); renamed {
							emit(sessID, event.SessionRename(sessID, name))
							// Refresh attached CLI clients' tab strip with the LLM title.
							rt.d.broadcastSessList()
						}
					}()
				}
				t := capture.NewTailer(sid, path, func(e event.Event) { emit(sid, e) }, onFirst).
					OnExchange(onExchange)
				// Part B: a resumed session's transcript was partly tailed+emitted
				// before the restart. `claude --resume <id>` APPENDS to the same
				// <id>.jsonl without rewriting prior rows, so seed the tailer at the
				// persisted offset — it replays nothing already streamed (no duplicate)
				// yet emits every new turn exactly once (no dropped event). A FRESH
				// (non-resumed) session keeps the offset-0 default. Consume the offset
				// once so a later same-id reuse does not wrongly re-seed.
				if rt.resumeIDs[sid] {
					t.SetOffset(rt.resumeOffset[sid])
					delete(rt.resumeIDs, sid)
				}
				stop := make(chan struct{})
				tailStops[sid] = stop
				// Track the tailer + the file id it is currently watching (its own tab
				// id) so a later post-/resume divergence can repoint it (BUG 2).
				tailers[sid] = t
				transcriptID[sid] = sid
				go t.Run(500*time.Millisecond, stop)
			}
			// Part B: record each live tailer's advanced offset + next seq into the
			// resume store as ONE unit (so offset and NextSeq always describe the same
			// moment). This only marks the store dirty — the debounced flusher writes
			// at most once per ~2s, so this is NOT an fsync per event. Resume-failure
			// fallback: a resumed child that exits within ~3s of spawn is treated as a
			// failed resume — emit done, drop it from the store, and spawn a FRESH
			// session in its place (done-before-start).
			for id, t := range tailers {
				rt.recordOffset(id, t.Offset())
			}
			rt.checkResumeFailures()
			// Clean up state for sessions that have ended: stop their tailer
			// goroutine and forget their announce/sync markers so the maps don't
			// grow without bound (leak #11). A reused id (new session) re-announces.
			//
			// A session that was announced to HQ and has now left the mux has
			// genuinely ended — natural exit, client ✕, or an HQ force shutdown that
			// closed the child (which makes the pump's onSessionExit remove it). Emit
			// a terminal status.change -> done so HQ retires the live row (#3). This
			// is the catch-all that keeps HQ's live list matching reality regardless
			// of HOW the session ended; the receiver's own done emit (#1/#2) covers
			// the instant case and ghosts the captureLoop never saw.
			for id, ch := range tailStops {
				if !live[id] {
					close(ch)
					delete(tailStops, id)
					delete(tailers, id)
					delete(transcriptID, id)
					// Drop the tab's topic state + checkpoint so the maps don't grow and a
					// reused id starts fresh (the session has genuinely ended).
					delete(topicState, id)
					if rt.topicStore != nil {
						rt.topicStore.Forget(id)
					}
				}
			}
			// Emit done for every announced session that has left the mux, even one
			// whose tailer never started, so no live row is ever orphaned. Tombstone
			// the id FIRST so any straggling event after it left the mux (a survivor
			// or a late hook) is suppressed and cannot revive the row (BUG 1); the
			// done emit itself is exempt from that suppression.
			for id := range announced {
				if !live[id] {
					// If this id was already retired earlier in this same cycle — a
					// failed resume handled by checkResumeFailures (tombstoned + done
					// emitted + fresh session spawned) — don't emit a SECOND done; just
					// finish the bookkeeping. isTerminated implies a done was already
					// sent (every markTerminated call site emits one).
					if !rt.isTerminated(id) {
						rt.markTerminated(id)
						emit(id, event.StatusChange(id, event.StatusActive, event.StatusDone))
					}
					delete(announced, id)
					rt.forgetSkillSync(id)
					// Part B: REMOVE from the resume store ONLY on a user-intent end
					// (FrameKill / control kill+shutdown, recorded via markUserKilled), so
					// a deliberately-killed session is not --resume-d next start. A
					// session that left the mux for any OTHER reason is preserved for
					// continuity. NOTE: a daemon restart/crash does not run this loop at
					// all (the process is gone), so this branch only ever fires for an
					// in-process departure.
					if rt.isUserKilled(id) {
						rt.removeFromStore(id)
					}
				}
			}
		}
		return false
	}

	for {
		if tick() {
			return
		}
	}
}

// loadTopicState restores a tab's checkpointed topic state, or a fresh State when
// no checkpoint exists (C6).
func (rt *Runtime) loadTopicState(tabID string) *topic.State {
	if rt.topicStore != nil {
		if s, ok := rt.topicStore.Load(tabID); ok {
			return &s
		}
	}
	return &topic.State{}
}

// checkpointTopic persists a tab's topic state after a fold/stash (C6).
// Best-effort: a write error is logged and swallowed so it can never block a turn.
func (rt *Runtime) checkpointTopic(tabID string, st *topic.State) {
	if rt.topicStore == nil || st == nil {
		return
	}
	if err := rt.topicStore.Save(tabID, *st); err != nil {
		diag.Logf("topic checkpoint: save %s: %v", tabID, err)
	}
}

// runTopicGate runs the cheap gate for a just-finished turn and, if it fires,
// builds the judge inputs, spawns the judge on a goroutine, folds the verdict, and
// emits session.topic + any session.learning through the same emit path
// session.rename uses (U6). It is called on Stop, after the tailer drain.
//
// The gate decision + the per-session state mutation happen synchronously under
// the tab's mutex (cheap), so the cadence/cap counters stay consistent. Only the
// model call runs on a goroutine OUTSIDE the lock, so the loop is never blocked by
// a slow judge; the verdict's Fold + emit re-acquire the lock briefly, serializing
// against the next turn's gate.
func (rt *Runtime) runTopicGate(tabID string, te *topicEntry, emit func(string, event.Event)) {
	// Re-parse the raw JSONL from the last-harvested offset to recover the turn's
	// write-class touched-file set + transcript slice (A4): argsSummary is lossy.
	path, err := capture.TranscriptPath(rt.d.repoRoot, tabID)
	if err != nil {
		return
	}

	te.mu.Lock()
	harvest, err := capture.HarvestTurn(path, te.st.LastOffset)
	if err != nil {
		te.mu.Unlock()
		return
	}
	te.st.LastOffset = harvest.EndOffset

	turnTokens := tokenEstimate(harvest.LatestUserPrompt, harvest.AssistantTail)
	decision := te.st.Gate(topic.GateInput{FilesTouched: harvest.FilesTouched, TurnTokens: turnTokens})
	if !decision.Fire {
		snap := *te.st
		te.mu.Unlock()
		rt.checkpointTopic(tabID, &snap)
		return
	}
	// Daemon-wide concurrency cap (D3): skip this turn's judge when all permits are
	// in use rather than queueing — the cadence floor re-fires on a later turn.
	if !rt.judgeLimit.TryAcquire() {
		snap := *te.st
		te.mu.Unlock()
		diag.Logf("topic gate: judge concurrency cap reached, skipping turn for %s", tabID)
		rt.checkpointTopic(tabID, &snap)
		return
	}

	// Snapshot the carried state + inputs for the (out-of-lock) judge call.
	slice := topic.BuildTranscriptSlice(harvest.LatestUserPrompt, harvest.AssistantTail)
	files := append([]string(nil), harvest.FilesTouched...)
	curLabel, curDesc := te.st.TopicLabel, te.st.Description
	te.mu.Unlock()

	docRef, docContents := topic.NearestDoc(rt.d.repoRoot, files)
	in := judge.Input{
		CurrentTopicLabel:  curLabel,
		CurrentDescription: curDesc,
		TranscriptSlice:    slice,
		FilesTouched:       files,
		NearestDoc:         docContents,
	}

	go func() {
		defer diag.Recover("runtime.topicJudge")
		defer rt.judgeLimit.Release()
		verdict, parseFailed := runJudge(in)
		if parseFailed {
			// Observable failure: keep the current topic, emit nothing, but surface the
			// dropped learning so loss is visible, not silent.
			diag.Logf("topic judge: parse failure for %s — kept current topic, dropped learning", tabID)
			return
		}
		te.mu.Lock()
		res := te.st.Fold(verdict, files, docRef)
		snap := *te.st
		te.mu.Unlock()
		// session.topic always (folds into the projection; never renames the slug).
		emit(tabID, event.SessionTopic(tabID, res.SegmentID, res.TopicLabel, res.Description))
		for _, l := range res.Learnings {
			emit(tabID, event.SessionLearning(tabID, res.SegmentID, res.TopicLabel, l.Stream, l.Text, l.DocRef, res.TurnID))
		}
		rt.checkpointTopic(tabID, &snap)
	}()
}

// tokenEstimate is the cheap ~4-chars/token heuristic used for the cadence floor's
// token accounting (the transcript rows carry server usage, but the gate only
// needs a rough turn size, not an exact count).
func tokenEstimate(parts ...string) int64 {
	var n int64
	for _, p := range parts {
		n += int64(len(p) / 4)
	}
	return n
}

// projectIDFor derives a stable, readable project id from the repo path (its
// base directory name, slugified). Sessions in HQ are grouped under this id.
func projectIDFor(repoRoot string) string {
	base := strings.ToLower(filepath.Base(repoRoot))
	var b strings.Builder
	prevDash := false
	for _, r := range base {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			prevDash = false
		} else if !prevDash {
			b.WriteByte('-')
			prevDash = true
		}
	}
	s := strings.Trim(b.String(), "-")
	if s == "" {
		return "project"
	}
	return s
}

// gitRemoteURL returns the origin remote URL for repoRoot, or ("", false) if
// there is no git repo / no origin remote / git is unavailable. It is a package
// var so tests can stub it without invoking real git.
var gitRemoteURL = func(repoRoot string) (string, bool) {
	cmd := exec.Command("git", "-C", repoRoot, "remote", "get-url", "origin")
	out, err := cmd.Output()
	if err != nil {
		return "", false
	}
	url := strings.TrimSpace(string(out))
	if url == "" {
		return "", false
	}
	return url, true
}

// repoNameFor derives a human-readable repo display name for repoRoot. It
// prefers the git origin remote reduced to "owner/repo" (host and trailing
// .git stripped); when there is no usable remote it falls back to the repo
// folder's base name. This is the value carried on session.start's `repo`
// field and used by HQ as the Project's display name + repo.
func repoNameFor(repoRoot string) string {
	if url, ok := gitRemoteURL(repoRoot); ok {
		if name := ownerRepoFromRemote(url); name != "" {
			return name
		}
	}
	return filepath.Base(repoRoot)
}

// ownerRepoFromRemote reduces a git remote URL to "owner/repo", stripping the
// scheme/host and any trailing ".git". It handles both SCP-style
// ("git@github.com:owner/repo.git") and URL-style
// ("https://github.com/owner/repo.git") remotes. Returns "" if it can't
// extract a sensible owner/repo pair.
func ownerRepoFromRemote(url string) string {
	s := strings.TrimSpace(url)
	s = strings.TrimSuffix(s, "/")
	// Drop a trailing ".git" suffix.
	s = strings.TrimSuffix(s, ".git")
	if s == "" {
		return ""
	}
	// Normalize separators: SCP form uses ':' after the host; URL form uses '/'.
	// Strip an explicit scheme first (e.g. "https://", "ssh://", "git://").
	if i := strings.Index(s, "://"); i >= 0 {
		s = s[i+3:]
		// Strip optional "user@" before host.
		if at := strings.Index(s, "@"); at >= 0 {
			s = s[at+1:]
		}
	} else if at := strings.Index(s, "@"); at >= 0 {
		// SCP-style "git@host:owner/repo".
		s = s[at+1:]
	}
	// At this point s is "host[:/]owner/repo...". Replace the first ':' with '/'
	// so the path splits uniformly.
	s = strings.Replace(s, ":", "/", 1)
	parts := strings.Split(s, "/")
	// Drop empty segments (e.g. from leading host// artifacts).
	clean := parts[:0]
	for _, p := range parts {
		if p != "" {
			clean = append(clean, p)
		}
	}
	if len(clean) < 3 {
		// Need at least host + owner + repo; otherwise no meaningful owner/repo.
		if len(clean) == 2 {
			// No host segment (e.g. already "owner/repo"): take it as-is.
			return clean[0] + "/" + clean[1]
		}
		return ""
	}
	// Last two segments are owner/repo; everything before is host/path.
	owner := clean[len(clean)-2]
	repo := clean[len(clean)-1]
	return owner + "/" + repo
}

// installHooks resolves this binary's path and writes the managed hooks block
// so Claude Code pipes lifecycle events to `"<exe>" __hook`. The hook shim then
// forwards each event to this repo's daemon socket. Best-effort: any failure
// (no resolvable executable, unwritable ~/.claude/settings.json) is ignored —
// the transcript tailer remains the authoritative event source.
func installHooks(repoRoot string) {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	// Install the managed hooks block into THIS repo's per-project config root (the
	// same root its sessions launch against), creating + seeding it as needed.
	dir, err := config.EnsureConfigDir(repoRoot)
	if err != nil {
		return
	}
	hookCmd := strconv.Quote(exe) + " __hook"
	_, _ = capture.InstallHooks(dir, hookCmd)
}

// Stop tears down the runtime.
func (rt *Runtime) Stop() {
	if rt == nil {
		return
	}
	close(rt.stop)
	if rt.client != nil {
		rt.client.Stop()
	}
}
