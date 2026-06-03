package daemon

import (
	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// StatusSnapshot is the daemon-wide meter rendered by the desktop status bar:
// cumulative tokens and cost across all sessions, plus the agents/skills drift
// count maintained by the config layer.
type StatusSnapshot struct {
	Tokens  int64   `json:"tokens"`
	CostUSD float64 `json:"costUsd"`
	Drift   int     `json:"drift"`
}

// updateStatus folds a cost.tick envelope into the running totals. Other event
// kinds do not affect the meters. Called from PublishEvent.
func (d *Daemon) updateStatus(env event.Envelope) {
	if env.Event.Kind != event.KindCostTick {
		return
	}
	d.statusMu.Lock()
	if env.Event.Tokens != nil {
		d.sTokens += *env.Event.Tokens
	}
	if env.Event.DeltaUsd != nil {
		d.sUSD += *env.Event.DeltaUsd
	}
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
	report, err := config.ComputeDrift(src)
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
	return StatusSnapshot{Tokens: d.sTokens, CostUSD: d.sUSD, Drift: d.sDrift}
}
