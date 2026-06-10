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
	KindSessionStart     Kind = "session.start"
	KindSessionRename    Kind = "session.rename"
	KindUserMsg          Kind = "user.msg"
	KindAssistantMsg     Kind = "assistant.msg"
	KindToolCall         Kind = "tool.call"
	KindToolResult       Kind = "tool.result"
	KindStatusChange     Kind = "status.change"
	KindSessionHeartbeat Kind = "session.heartbeat"
	KindSessionTopic     Kind = "session.topic"
	KindSessionLearning  Kind = "session.learning"
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

	// user.msg / assistant.msg
	Tokens *int64 `json:"tokens,omitempty"`

	// user.msg / assistant.msg: the actual (truncated) turn text. Optional; older
	// daemons omitted it. Carries the real content HQ renders in the live feed.
	Text string `json:"text,omitempty"`

	// status.change
	From Status `json:"from,omitempty"`
	To   Status `json:"to,omitempty"`

	// session.topic / session.learning (topic-focus logging). All omitempty so
	// they never appear on other kinds, keeping existing golden fixtures byte-stable.
	SegmentID  string `json:"segmentId,omitempty"`
	TopicLabel string `json:"topicLabel,omitempty"`
	// session.topic: rich, self-contained rolling description of the current topic.
	Description string `json:"description,omitempty"`
	// session.learning: stream ("impl"|"doc"), docRef (doc stream only), and the
	// idempotency key turnId. The learning payload reuses the shared Text field.
	Stream string `json:"stream,omitempty"`
	DocRef string `json:"docRef,omitempty"`
	TurnID string `json:"turnId,omitempty"`
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

// boolPtr / int64Ptr are small constructors for the optional fields.
func boolPtr(b bool) *bool    { return &b }
func int64Ptr(i int64) *int64 { return &i }

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

// UserMsgText builds a user.msg event carrying the actual (already-truncated)
// user-turn text alongside the token count. Text may be empty (omitted on the
// wire via omitempty).
func UserMsgText(sessionID string, tokens int64, text string) Event {
	return Event{Kind: KindUserMsg, SessionID: sessionID, Tokens: int64Ptr(tokens), Text: text}
}

// AssistantMsgText builds an assistant.msg event carrying the actual
// (already-truncated) assistant-reply text alongside the token count.
func AssistantMsgText(sessionID string, tokens int64, text string) Event {
	return Event{Kind: KindAssistantMsg, SessionID: sessionID, Tokens: int64Ptr(tokens), Text: text}
}

// ToolCall builds a tool.call event.
func ToolCall(sessionID, tool, argsSummary string) Event {
	return Event{Kind: KindToolCall, SessionID: sessionID, Tool: tool, ArgsSummary: argsSummary}
}

// ToolResult builds a tool.result event.
func ToolResult(sessionID string, ok bool, ms int64, summary string) Event {
	return Event{Kind: KindToolResult, SessionID: sessionID, OK: boolPtr(ok), Ms: int64Ptr(ms), Summary: summary}
}

// StatusChange builds a status.change event.
func StatusChange(sessionID string, from, to Status) Event {
	return Event{Kind: KindStatusChange, SessionID: sessionID, From: from, To: to}
}

// SessionHeartbeat builds a session.heartbeat event. The daemon emits these
// periodically for each live session so HQ can bump lastEventAt and keep a
// genuinely-alive idle session live; absence of heartbeats lets read-time
// freshness drop a powered-off laptop's sessions.
func SessionHeartbeat(sessionID string) Event {
	return Event{Kind: KindSessionHeartbeat, SessionID: sessionID}
}

// SessionTopic builds a session.topic event carrying the current topic label and
// a rich, self-contained rolling description. The backend folds these into the
// session projection (topic + description), leaving the stable name untouched.
func SessionTopic(sessionID, segmentID, topicLabel, description string) Event {
	return Event{
		Kind: KindSessionTopic, SessionID: sessionID,
		SegmentID: segmentID, TopicLabel: topicLabel, Description: description,
	}
}

// SessionLearning builds a session.learning event — an append record mined from a
// correction turn. stream is "impl" or "doc"; docRef is set only on the doc
// stream; turnID is the idempotency key ((sessionId, turnId) dedupes at ingest).
func SessionLearning(sessionID, segmentID, topicLabel, stream, text, docRef, turnID string) Event {
	return Event{
		Kind: KindSessionLearning, SessionID: sessionID,
		SegmentID: segmentID, TopicLabel: topicLabel, Stream: stream,
		Text: text, DocRef: docRef, TurnID: turnID,
	}
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
	case KindStatusChange:
		if !ValidStatus(e.From) || !ValidStatus(e.To) {
			return fmt.Errorf("status.change: from/to must be valid statuses")
		}
	case KindSessionHeartbeat:
		// Only sessionId is required (checked above).
	case KindSessionTopic:
		if e.SegmentID == "" || e.TopicLabel == "" {
			return fmt.Errorf("session.topic: segmentId and topicLabel required")
		}
	case KindSessionLearning:
		if e.SegmentID == "" || e.TopicLabel == "" || e.Text == "" || e.TurnID == "" {
			return fmt.Errorf("session.learning: segmentId, topicLabel, text, turnId required")
		}
		if e.Stream != "impl" && e.Stream != "doc" {
			return fmt.Errorf("session.learning: stream must be impl or doc")
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
