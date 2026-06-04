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
	"github.com/workflow-harness/claude-plus/internal/title"
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

	// cfgSrc is HQ's effective (org+user+project resolved) skills/agents source,
	// set when an HQ REST base is configured. Used both for the drift meter and
	// the per-session skills auto-sync (#1). nil when HQ REST is unconfigured.
	cfgSrc config.RemoteSource

	// syncedMu guards syncedSessions, which records which sessions have already
	// triggered the one-shot skills auto-sync (so it runs once per NEW session).
	syncedMu       sync.Mutex
	syncedSessions map[string]bool
}

// emit wraps a captured event in an envelope and fans it to local subscribers
// (the Stream panel) plus HQ when configured. It is the single send path shared
// by the transcript tailer and the hook receiver (U18), so hook-sourced and
// tailer-sourced events are sequenced and delivered identically.
func (rt *Runtime) emit(sid string, e event.Event) {
	env := event.Envelope{
		V: 1, InstanceID: rt.instanceID, Host: rt.host,
		TS: time.Now().UnixMilli(), Seq: rt.seq.Next(sid), Event: e,
	}
	rt.d.PublishEvent(env) // local subscribers (Stream panel)
	if rt.client != nil {
		_ = rt.client.Send(env) // HQ, when configured
	}
}

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
	rt := &Runtime{d: d, seq: transport.NewSeq(), stop: make(chan struct{}),
		instanceID: instanceID, host: hostName(),
		syncedSessions: map[string]bool{}}

	// Route hook-shim events through the same emit path as the tailer (U18).
	d.SetHookIngestor(rt.emit)

	// Install the managed hooks block in ~/.claude/settings.json so Claude Code
	// forwards lifecycle events to `claude+ __hook` (which delivers them to this
	// daemon's socket). Best-effort: a missing executable path or unwritable
	// settings file must never block daemon startup, so errors are ignored.
	installHooks()

	if cfg, ok := loadHQConfig(); ok {
		home, _ := os.UserHomeDir()
		if buf, err := transport.OpenRingBuffer(filepath.Join(home, ".claude-plus", "outbound.jsonl")); err == nil {
			recv := d.NewControlReceiver()
			// When HQ force-shuts-down a session (or a ghost from a dead daemon),
			// emit a terminal status.change -> done so HQ drops it from the live
			// list and the row the user clicked actually disappears (#1, #2).
			recv.Terminated = func(sessionID string) {
				rt.emit(sessionID, event.StatusChange(sessionID, event.StatusActive, event.StatusDone))
			}
			rt.client = transport.NewClient(cfg.URL, cfg.Token, instanceID, buf, recv.Handle)
			go rt.client.Run()
		}
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
	report, err := config.ComputeDrift(src)
	if err != nil {
		diag.Logf("skills auto-sync: compute drift failed: %v", err)
		return
	}
	local, err := config.ReadLocal()
	if err != nil {
		diag.Logf("skills auto-sync: read local failed: %v", err)
		return
	}
	pulled, pushed, errs := config.Reconcile(report, src, local)
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
// reconcile the daemon runs automatically per session. Returns counts actuated.
func SyncSkillsNow(repoRoot string) (pulled, pushed int, err error) {
	base, ok := loadAPIBase()
	if !ok {
		return 0, 0, fmt.Errorf("no HQ API base configured (run `claude+ login`)")
	}
	cfg, ok := loadHQConfig()
	if !ok {
		return 0, 0, fmt.Errorf("not signed in to HQ (run `claude+ login`)")
	}
	src := config.NewHTTPRemoteSource(base, cfg.Token, projectIDFor(repoRoot))
	report, err := config.ComputeDrift(src)
	if err != nil {
		return 0, 0, err
	}
	local, err := config.ReadLocal()
	if err != nil {
		return 0, 0, err
	}
	pulled, pushed, errs := config.Reconcile(report, src, local)
	if len(errs) > 0 {
		return pulled, pushed, errs[0]
	}
	return pulled, pushed, nil
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
	_ = rt.d.SyncConfigOnce(src) // prime immediately on start
	for {
		select {
		case <-rt.stop:
			return
		case <-tk.C:
			_ = rt.d.SyncConfigOnce(src)
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

	for {
		select {
		case <-rt.stop:
			closeAllTails()
			return
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
				stop := make(chan struct{})
				tailStops[sid] = stop
				go t.Run(500*time.Millisecond, stop)
			}
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
				}
			}
			// Emit done for every announced session that has left the mux, even one
			// whose tailer never started, so no live row is ever orphaned.
			for id := range announced {
				if !live[id] {
					emit(id, event.StatusChange(id, event.StatusActive, event.StatusDone))
					delete(announced, id)
					rt.forgetSkillSync(id)
				}
			}
		}
	}
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
func installHooks() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	hookCmd := strconv.Quote(exe) + " __hook"
	_, _ = capture.InstallHooks(hookCmd)
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
