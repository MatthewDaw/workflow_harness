package daemon

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/workflow-harness/claude-plus/internal/capture"
	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/event"
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
		instanceID: instanceID, host: hostName()}

	// Route hook-shim events through the same emit path as the tailer (U18).
	d.SetHookIngestor(rt.emit)

	if cfg, ok := loadHQConfig(); ok {
		home, _ := os.UserHomeDir()
		if buf, err := transport.OpenRingBuffer(filepath.Join(home, ".claude-plus", "outbound.jsonl")); err == nil {
			recv := d.NewControlReceiver()
			rt.client = transport.NewClient(cfg.URL, cfg.Token, instanceID, buf, recv.Handle)
			go rt.client.Run()
		}
	}

	// Per-session transcript tailers feed the envelope stream (local bus + HQ).
	go rt.captureLoop(instanceID)

	// Agents/skills drift meter (U19): poll HQ's effective registry and fold the
	// drift count into the status snapshot. Only runs when an HQ REST base is
	// configured; otherwise the meter stays at 0 (no remote to compare against).
	if base, ok := loadAPIBase(); ok {
		if cfg, ok := loadHQConfig(); ok {
			src := config.NewHTTPRemoteSource(base, cfg.Token, projectIDFor(d.repoRoot))
			go rt.configSyncLoop(src)
		}
	}
	return rt
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
	tailed := map[string]*capture.Tailer{}
	announced := map[string]bool{}
	tk := time.NewTicker(time.Second)
	defer tk.Stop()
	host := rt.host
	projectID := projectIDFor(rt.d.repoRoot)
	emit := rt.emit

	for {
		select {
		case <-rt.stop:
			return
		case <-tk.C:
			for _, v := range rt.d.mux.List() {
				// Announce a newly-seen session with session.start (seq 0 for this
				// session) so HQ has its identity — project, name — from the
				// first event, before any transcript activity.
				if !announced[v.ID] {
					announced[v.ID] = true
					emit(v.ID, event.SessionStart(v.ID, projectID, host, v.Name, ""))
				}
				if _, ok := tailed[v.ID]; ok {
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
					}
				}
				t := capture.NewTailer(sid, path, func(e event.Event) { emit(sid, e) }, onFirst)
				tailed[sid] = t
				go t.Run(500*time.Millisecond, rt.stop)
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
