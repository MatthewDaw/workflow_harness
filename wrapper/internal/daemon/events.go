package daemon

import "github.com/workflow-harness/claude-plus/internal/event"

// maxRecentEvents bounds the per-daemon replay buffer. A freshly-attached client
// (e.g. the desktop Stream panel) gets the last N envelopes immediately so the
// panel is populated on open rather than blank until the next event.
const maxRecentEvents = 200

// PublishEvent fans an envelope to every local subscriber and records it in the
// bounded replay buffer. The Runtime calls this for every captured event,
// independent of whether HQ credentials exist — local subscribers (attach
// clients) see events even with no `claude+ login`.
func (d *Daemon) PublishEvent(env event.Envelope) {
	d.evMu.Lock()
	d.recent = append(d.recent, env)
	if len(d.recent) > maxRecentEvents {
		d.recent = d.recent[len(d.recent)-maxRecentEvents:]
	}
	sinks := make([]func(event.Envelope), 0, len(d.eventSinks))
	for _, s := range d.eventSinks {
		sinks = append(sinks, s)
	}
	d.evMu.Unlock()
	for _, s := range sinks {
		s(env)
	}
}

// AddEventSink registers a subscriber and immediately replays the recent buffer
// to it, mirroring the output-history replay in pty.Mux.AddSink.
func (d *Daemon) AddEventSink(id string, fn func(event.Envelope)) {
	d.evMu.Lock()
	d.eventSinks[id] = fn
	recent := append([]event.Envelope(nil), d.recent...)
	d.evMu.Unlock()
	for _, env := range recent {
		fn(env)
	}
}

// RemoveEventSink deregisters a subscriber.
func (d *Daemon) RemoveEventSink(id string) {
	d.evMu.Lock()
	delete(d.eventSinks, id)
	d.evMu.Unlock()
}
