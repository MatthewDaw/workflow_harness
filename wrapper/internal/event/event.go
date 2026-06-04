// Package event re-declares the @harness/shared event envelope contract in Go.
//
// This is the cross-language wire format the claude+ wrapper emits and the
// backend ingests. The canonical definition lives in TypeScript at
// packages/shared/src/events.ts; both sides are kept honest by the golden
// fixture at packages/shared/test/golden/event-envelope.json, which the tests
// in this package parse and round-trip.
//
// Field names below MUST match the JSON keys produced by the TS zod schemas
// exactly (camelCase), or golden-fixture parity breaks.
package event

import (
	"encoding/json"
	"fmt"
)

// Status is a session lifecycle status. Mirrors SESSION_STATUSES in TS.
type Status string

const (
	StatusActive     Status = "active"
	StatusNeedsInput Status = "needs_input"
	StatusIdle       Status = "idle"
	StatusDone       Status = "done"
)

// ValidStatus reports whether s is one of the known statuses.
func ValidStatus(s Status) bool {
	switch s {
	case StatusActive, StatusNeedsInput, StatusIdle, StatusDone:
		return true
	default:
		return false
	}
}

// Kind enumerates the event discriminator values.
type Kind string

const (
	KindSessionStart  Kind = "session.start"
	KindSessionRename Kind = "session.rename"
	KindUserMsg       Kind = "user.msg"
	KindAssistantMsg  Kind = "assistant.msg"
	KindToolCall      Kind = "tool.call"
	KindToolResult    Kind = "tool.result"
	KindCostTick      Kind = "cost.tick"
	KindStatusChange  Kind = "status.change"
)

// Event is the discriminated union of every event kind. It is modeled as a flat
// struct with a Kind discriminator and pointer/optional fields so it can both
// marshal and unmarshal to the same JSON shape the TS discriminated union uses.
// Only the fields relevant to Kind are populated; the rest stay at their zero
// value and are omitted from JSON via omitempty where the TS schema treats them
// as absent.
type Event struct {
	Kind Kind `json:"kind"`

	// Common to every event except none — every kind carries sessionId.
	SessionID string `json:"sessionId"`

	// session.start
	ProjectID string `json:"projectId,omitempty"`
	Host      string `json:"host,omitempty"`
	Name      string `json:"name,omitempty"`
	Agent     string `json:"agent,omitempty"`
	// Repo is the human/repo display name (git "owner/repo" or repo folder name).
	// Optional; mirrors the optional `repo` field in the TS session.start schema.
	Repo string `json:"repo,omitempty"`

	// tool.call
	Tool        string `json:"tool,omitempty"`
	ArgsSummary string `json:"argsSummary,omitempty"`

	// tool.result / session.rename
	OK      *bool  `json:"ok,omitempty"`
	Ms      *int64 `json:"ms,omitempty"`
	Summary string `json:"summary,omitempty"`

	// user.msg / assistant.msg / cost.tick
	Tokens *int64 `json:"tokens,omitempty"`

	// cost.tick
	DeltaUsd *float64 `json:"deltaUsd,omitempty"`
	TotalUsd *float64 `json:"totalUsd,omitempty"`

	// status.change
	From Status `json:"from,omitempty"`
	To   Status `json:"to,omitempty"`
}

// Envelope wraps every event sent from a daemon to HQ. Mirrors envelopeSchema.
type Envelope struct {
	V          int    `json:"v"`
	InstanceID string `json:"instanceId"`
	Host       string `json:"host"`
	TS         int64  `json:"ts"`  // epoch milliseconds
	Seq        int64  `json:"seq"` // monotonic per session
	Event      Event  `json:"event"`
}

// boolPtr / int64Ptr / f64Ptr are small constructors for the optional fields.
func boolPtr(b bool) *bool        { return &b }
func int64Ptr(i int64) *int64     { return &i }
func f64Ptr(f float64) *float64   { return &f }

// ----- Event constructors (kept parallel to the TS schemas) -----

// SessionStart builds a session.start event. repo is the human-readable repo
// display name (git "owner/repo" or the repo folder name); it may be empty, in
// which case it is omitted from the JSON (older-daemon-compatible).
func SessionStart(sessionID, projectID, host, name string, agent string, repo string) Event {
	return Event{
		Kind: KindSessionStart, SessionID: sessionID, ProjectID: projectID,
		Host: host, Name: name, Agent: agent, Repo: repo,
	}
}

// SessionRename builds a session.rename event.
func SessionRename(sessionID, name string) Event {
	return Event{Kind: KindSessionRename, SessionID: sessionID, Name: name}
}

// SessionRenameWithSummary builds a session.rename event that also carries the
// first-prompt summary (the raw first prompt the user typed). The backend
// projection folds summary into the session read model alongside the name.
func SessionRenameWithSummary(sessionID, name, summary string) Event {
	return Event{Kind: KindSessionRename, SessionID: sessionID, Name: name, Summary: summary}
}

// UserMsg builds a user.msg event.
func UserMsg(sessionID string, tokens int64) Event {
	return Event{Kind: KindUserMsg, SessionID: sessionID, Tokens: int64Ptr(tokens)}
}

// AssistantMsg builds an assistant.msg event.
func AssistantMsg(sessionID string, tokens int64) Event {
	return Event{Kind: KindAssistantMsg, SessionID: sessionID, Tokens: int64Ptr(tokens)}
}

// ToolCall builds a tool.call event.
func ToolCall(sessionID, tool, argsSummary string) Event {
	return Event{Kind: KindToolCall, SessionID: sessionID, Tool: tool, ArgsSummary: argsSummary}
}

// ToolResult builds a tool.result event.
func ToolResult(sessionID string, ok bool, ms int64, summary string) Event {
	return Event{Kind: KindToolResult, SessionID: sessionID, OK: boolPtr(ok), Ms: int64Ptr(ms), Summary: summary}
}

// CostTick builds a cost.tick event.
func CostTick(sessionID string, deltaUsd, totalUsd float64, tokens int64) Event {
	return Event{
		Kind: KindCostTick, SessionID: sessionID,
		DeltaUsd: f64Ptr(deltaUsd), TotalUsd: f64Ptr(totalUsd), Tokens: int64Ptr(tokens),
	}
}

// StatusChange builds a status.change event.
func StatusChange(sessionID string, from, to Status) Event {
	return Event{Kind: KindStatusChange, SessionID: sessionID, From: from, To: to}
}

// Validate checks that an event has the required fields for its kind. It mirrors
// the zod refinements on the TS side (non-empty strings, valid statuses, etc.).
func (e Event) Validate() error {
	if e.SessionID == "" {
		return fmt.Errorf("event %q: sessionId required", e.Kind)
	}
	switch e.Kind {
	case KindSessionStart:
		if e.ProjectID == "" || e.Host == "" || e.Name == "" {
			return fmt.Errorf("session.start: projectId, host, name required")
		}
	case KindSessionRename:
		if e.Name == "" {
			return fmt.Errorf("session.rename: name required")
		}
	case KindUserMsg, KindAssistantMsg:
		if e.Tokens == nil {
			return fmt.Errorf("%s: tokens required", e.Kind)
		}
	case KindToolCall:
		if e.Tool == "" {
			return fmt.Errorf("tool.call: tool required")
		}
	case KindToolResult:
		if e.OK == nil || e.Ms == nil {
			return fmt.Errorf("tool.result: ok and ms required")
		}
	case KindCostTick:
		if e.DeltaUsd == nil || e.TotalUsd == nil || e.Tokens == nil {
			return fmt.Errorf("cost.tick: deltaUsd, totalUsd, tokens required")
		}
	case KindStatusChange:
		if !ValidStatus(e.From) || !ValidStatus(e.To) {
			return fmt.Errorf("status.change: from/to must be valid statuses")
		}
	default:
		return fmt.Errorf("unknown event kind %q", e.Kind)
	}
	return nil
}

// Validate checks an envelope's invariants.
func (env Envelope) Validate() error {
	if env.V != 1 {
		return fmt.Errorf("envelope: v must be 1, got %d", env.V)
	}
	if env.InstanceID == "" || env.Host == "" {
		return fmt.Errorf("envelope: instanceId and host required")
	}
	if env.TS < 0 || env.Seq < 0 {
		return fmt.Errorf("envelope: ts and seq must be non-negative")
	}
	return env.Event.Validate()
}

// ParseEnvelope unmarshals and validates an envelope from JSON bytes.
func ParseEnvelope(b []byte) (Envelope, error) {
	var env Envelope
	if err := json.Unmarshal(b, &env); err != nil {
		return Envelope{}, err
	}
	if err := env.Validate(); err != nil {
		return Envelope{}, err
	}
	return env, nil
}

// Marshal serializes an envelope to JSON bytes.
func (env Envelope) Marshal() ([]byte, error) {
	return json.Marshal(env)
}
