package daemon

import (
	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// StatusSnapshot is the daemon-wide meter rendered by the status bar: cumulative
// tokens across all sessions, plus the agents/skills drift count maintained by
// the config layer.
type StatusSnapshot struct {
	Tokens int64 `json:"tokens"`
	Drift  int   `json:"drift"`
}

// updateStatus folds an event's token count into the running total. Only the
// message events (user.msg / assistant.msg) carry tokens; every other kind is
// ignored. Called from PublishEvent.
func (d *Daemon) updateStatus(env event.Envelope) {
	switch env.Event.Kind {
	case event.KindUserMsg, event.KindAssistantMsg:
	default:
		return
	}
	if env.Event.Tokens == nil {
		return
	}
	d.statusMu.Lock()
	d.sTokens += *env.Event.Tokens
	d.statusMu.Unlock()
}

// SetDrift records the current agents/skills drift count (config layer, Phase 5).
func (d *Daemon) SetDrift(n int) {
	d.statusMu.Lock()
	d.sDrift = n
	d.statusMu.Unlock()
}

// SyncConfigOnce computes agents/skills drift against HQ and folds the count into
// the status meter (U19). It is the single tick the runtime's sync loop repeats;
// a fetch error leaves the prior count untouched (transient HQ blips don't blank
// the meter). Returns the error for the caller to log.
func (d *Daemon) SyncConfigOnce(src config.RemoteSource) error {
	plus, err := config.ProjectConfigDir(d.repoRoot)
	if err != nil {
		return err
	}
	report, err := config.ComputeDrift(src, plus)
	if err != nil {
		return err
	}
	d.SetDrift(report.DriftCount())
	return nil
}

// Status returns the current meter snapshot.
func (d *Daemon) Status() StatusSnapshot {
	d.statusMu.Lock()
	defer d.statusMu.Unlock()
	return StatusSnapshot{Tokens: d.sTokens, Drift: d.sDrift}
}
