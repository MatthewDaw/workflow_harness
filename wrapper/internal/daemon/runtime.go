package daemon

import (
	"os"
	"path/filepath"
	"time"

	"github.com/workflow-harness/claude-plus/internal/capture"
	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/transport"
)

// Runtime ties the daemon's session mux to the capture and transport layers: it
// tails each session's transcript, maps activity to envelopes, applies auto
// names, and streams envelopes outbound with offline buffering + control
// receive. It is started by the detached daemon process when HQ credentials are
// present; without credentials the daemon still hosts sessions locally.
type Runtime struct {
	d      *Daemon
	client *transport.Client
	seq    *transport.Seq
	stop   chan struct{}
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

// StartRuntime wires capture+transport for a daemon if HQ is configured. It is a
// no-op (returns nil) when no credentials are present, so local-only use keeps
// working. The returned Runtime is stopped on daemon shutdown.
func StartRuntime(d *Daemon, instanceID string) *Runtime {
	cfg, ok := loadHQConfig()
	if !ok {
		return nil
	}

	home, _ := os.UserHomeDir()
	buf, err := transport.OpenRingBuffer(filepath.Join(home, ".claude-plus", "outbound.jsonl"))
	if err != nil {
		return nil
	}

	rt := &Runtime{d: d, seq: transport.NewSeq(), stop: make(chan struct{})}
	recv := d.NewControlReceiver()
	rt.client = transport.NewClient(cfg.URL, cfg.Token, buf, recv.Handle)
	go rt.client.Run()

	// Per-session transcript tailers feed the envelope stream.
	go rt.captureLoop(instanceID)
	return rt
}

// captureLoop spawns a transcript tailer for each session and forwards its
// envelopes outbound. Sessions appearing later are picked up on the poll tick.
func (rt *Runtime) captureLoop(instanceID string) {
	tailed := map[string]*capture.Tailer{}
	tk := time.NewTicker(time.Second)
	defer tk.Stop()
	host := hostName()

	for {
		select {
		case <-rt.stop:
			return
		case <-tk.C:
			for _, v := range rt.d.mux.List() {
				if _, ok := tailed[v.ID]; ok {
					continue
				}
				path, err := capture.TranscriptPath(rt.d.repoRoot, v.ID)
				if err != nil {
					continue
				}
				sid := v.ID
				emit := func(e event.Event) {
					env := event.Envelope{
						V: 1, InstanceID: instanceID, Host: host,
						TS: time.Now().UnixMilli(), Seq: rt.seq.Next(sid), Event: e,
					}
					_ = rt.client.Send(env)
				}
				onFirst := func(sessID, text string) {
					if renamed, name := rt.d.mux.ApplyAutoName(sessID, text); renamed {
						emit(event.SessionRename(sessID, name))
					}
				}
				t := capture.NewTailer(sid, path, emit, onFirst)
				tailed[sid] = t
				go t.Run(500*time.Millisecond, rt.stop)
			}
		}
	}
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
