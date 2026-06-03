package transport

import (
	"errors"
	"fmt"
	"net/url"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"github.com/workflow-harness/claude-plus/internal/diag"
	"github.com/workflow-harness/claude-plus/internal/event"
)

// HandshakeError is a dial failure that carries the HTTP status from the failed
// WebSocket upgrade. A 401/403 means HQ's authorizer rejected the device token —
// a PERMANENT failure that backoff/retry can never fix until the credentials
// change — so it is classified distinctly from transient network errors and
// surfaced rather than swallowed by the reconnect loop.
type HandshakeError struct {
	Status int
	Err    error
}

func (e *HandshakeError) Error() string {
	return fmt.Sprintf("websocket handshake rejected: HTTP %d (%v)", e.Status, e.Err)
}

func (e *HandshakeError) Unwrap() error { return e.Err }

// IsAuthRejection reports whether err is a handshake rejection caused by a bad
// or stale credential (HTTP 401/403). Transient errors (network, 5xx) are not
// auth rejections — retrying those is the right behavior.
func IsAuthRejection(err error) bool {
	var he *HandshakeError
	if errors.As(err, &he) {
		return he.Status == 401 || he.Status == 403
	}
	return false
}

// Seq is a monotonic per-session sequence generator. The wrapper assigns a
// strictly increasing seq to each envelope per session so HQ can detect gaps and
// dedupe on reconnect (R3 contract guard pairs with the golden fixture).
type Seq struct {
	mu   sync.Mutex
	next map[string]int64
}

// NewSeq creates a sequence generator.
func NewSeq() *Seq { return &Seq{next: map[string]int64{}} }

// Next returns the next seq for a session id.
func (s *Seq) Next(sessID string) int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := s.next[sessID]
	s.next[sessID] = n + 1
	return n
}

// ControlHandler applies an inbound control frame (defined in control.go).
type ControlHandler func(ControlFrame)

// Client is the outbound WebSocket client. It owns the ring buffer, replays
// pending envelopes on (re)connect, and dispatches inbound control frames to a
// handler. Networking is strictly outbound (KTD5/KTD6): no inbound ports.
type Client struct {
	url        string
	token      string // device token, sent on the handshake query string
	instanceID string // this daemon's instance id, for HQ → daemon control routing
	buf        *RingBuffer
	onCtrl     ControlHandler

	mu        sync.Mutex
	conn      *websocket.Conn
	connected atomic.Bool

	sendCh chan event.Envelope
	stopCh chan struct{}

	// dial is overridable in tests to inject a fake transport.
	dial func(url, token, instanceID string) (*websocket.Conn, error)

	// onDialError is invoked on every failed dial so a connection problem is not
	// silent. Defaults to logDialError (actionable diagnostic on auth rejection);
	// overridable in tests. authLogged dedups the auth-rejection diagnostic so the
	// backoff loop logs it once per outage, not every retry.
	onDialError func(error)
	authLogged  bool
}

// NewClient constructs a transport client. buf must be open; onCtrl receives
// control frames routed down from HQ. instanceID identifies this daemon so HQ can
// route steering commands back to it (the `$connect` handler indexes it).
func NewClient(url, token, instanceID string, buf *RingBuffer, onCtrl ControlHandler) *Client {
	c := &Client{
		url: url, token: token, instanceID: instanceID, buf: buf, onCtrl: onCtrl,
		sendCh: make(chan event.Envelope, 256),
		stopCh: make(chan struct{}),
		dial:   defaultDial,
	}
	c.onDialError = c.logDialError
	return c
}

// logDialError is the default dial-error handler. A stale/invalid token (HTTP
// 401/403) is a permanent failure retrying can't resolve, so it emits a single
// actionable diagnostic per outage. Transient errors (offline, 5xx) stay quiet —
// the on-disk ring buffer holds events until HQ is reachable again — so the log
// isn't spammed during normal disconnects.
func (c *Client) logDialError(err error) {
	if !IsAuthRejection(err) {
		return
	}
	if c.authLogged {
		return
	}
	c.authLogged = true
	diag.Logf("claude+: HQ rejected the device token (%v). Events are buffered locally; run `claude+ login` to re-authenticate.", err)
}

// defaultDial opens the WebSocket. API Gateway's `$connect` Lambda authorizer
// reads the credential from the handshake query string
// (`route.request.querystring.token`) — a WS upgrade can't carry custom headers
// reliably — so the token, role, and instanceId go on the URL, not in a header.
func defaultDial(rawURL, token, instanceID string) (*websocket.Conn, error) {
	dialURL := rawURL
	if u, err := url.Parse(rawURL); err == nil {
		q := u.Query()
		if token != "" {
			q.Set("token", token)
		}
		q.Set("role", "daemon")
		if instanceID != "" {
			q.Set("instanceId", instanceID)
		}
		u.RawQuery = q.Encode()
		dialURL = u.String()
	}
	conn, resp, err := websocket.DefaultDialer.Dial(dialURL, nil)
	if err != nil {
		// A failed upgrade (e.g. websocket.ErrBadHandshake) carries the HTTP
		// response; surface its status so the caller can tell a rejected token
		// (401/403) apart from a transient outage.
		if resp != nil {
			return nil, &HandshakeError{Status: resp.StatusCode, Err: err}
		}
		return nil, err
	}
	return conn, nil
}

// Send durably enqueues an envelope (it is buffered to disk first, then pushed).
// Safe to call whether or not HQ is currently reachable.
func (c *Client) Send(env event.Envelope) error {
	if err := c.buf.Append(env); err != nil {
		return err
	}
	select {
	case c.sendCh <- env:
	default:
		// Channel full: the run loop will pick it up from the buffer on its next
		// replay pass, so dropping the in-memory signal is safe.
	}
	return nil
}

// Connected reports whether the WS is currently up.
func (c *Client) Connected() bool { return c.connected.Load() }

// Run is the connection manager: dial, replay the buffer, then pump live events
// and read control frames, reconnecting with backoff on failure until Stop.
func (c *Client) Run() {
	backoff := time.Second
	for {
		select {
		case <-c.stopCh:
			return
		default:
		}
		conn, err := c.dial(c.url, c.token, c.instanceID)
		if err != nil {
			c.onDialError(err) // surface (don't swallow) the failure
			if !c.sleep(backoff) {
				return
			}
			if backoff < 30*time.Second {
				backoff *= 2
			}
			continue
		}
		backoff = time.Second
		c.authLogged = false // a fresh connection clears the prior auth diagnostic
		c.mu.Lock()
		c.conn = conn
		c.mu.Unlock()
		c.connected.Store(true)

		if err := c.replay(conn); err == nil {
			c.serve(conn)
		}

		c.connected.Store(false)
		_ = conn.Close()
	}
}

// frameWriter is the minimal write capability flush needs (*websocket.Conn
// satisfies it). Abstracting it lets the over-ack regression test drive flush
// with a fake that can fail mid-stream, deterministically, without a live socket.
type frameWriter interface {
	WriteJSON(v interface{}) error
}

// replay re-sends all unacknowledged envelopes in order, then acks them. This is
// the offline → online catch-up; ordering and seq are preserved by the buffer.
func (c *Client) replay(conn frameWriter) error {
	return c.flush(conn)
}

// flush writes every currently-unacknowledged envelope from the on-disk buffer
// (in order) and acks exactly the number SENT — never more. The buffer, not the
// in-memory sendCh, is the single source of truth for what has been delivered, so
// an envelope is acked exactly once: replay() and the live serve() loop both
// route through here, and sendCh is only a wake-up signal (its value is ignored).
//
// This closes data-loss #9: previously replay() acked len(pending) AND the live
// loop drained the same envelopes still queued in sendCh, acking 1 each over the
// now-compacted buffer, inflating 'acked' past the line count and permanently
// skipping later events. Acking only the count actually drained from the buffer
// keeps the cursor exact regardless of how many stale signals sendCh holds, and a
// mid-stream write failure acks only what went out (the rest stay pending).
func (c *Client) flush(conn frameWriter) error {
	pending, err := c.buf.Pending()
	if err != nil {
		return err
	}
	sent := 0
	for _, env := range pending {
		if err := conn.WriteJSON(eventFrame{Action: "event", Envelope: env}); err != nil {
			if sent > 0 {
				_ = c.buf.Ack(sent)
			}
			return err
		}
		sent++
	}
	if sent > 0 {
		return c.buf.Ack(sent)
	}
	return nil
}

// serve pumps live sends and reads control frames until the connection drops.
// Each sendCh tick wakes the loop to drain whatever is newly pending in the
// buffer; the channel value is intentionally ignored (the buffer is the source
// of truth) so a stale signal left over from replay can never double-ack.
func (c *Client) serve(conn *websocket.Conn) {
	readErr := make(chan struct{})
	go func() {
		defer close(readErr)
		for {
			var msg inMsg
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg.Type == "control" && c.onCtrl != nil {
				c.onCtrl(ControlFrame{SessionID: msg.SessionID, Action: msg.Action, Payload: msg.Payload})
			}
		}
	}()

	for {
		select {
		case <-c.stopCh:
			return
		case <-readErr:
			return
		case <-c.sendCh:
			if err := c.flush(conn); err != nil {
				return
			}
		}
	}
}

func (c *Client) sleep(d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-c.stopCh:
		return false
	case <-t.C:
		return true
	}
}

// Stop terminates the run loop and closes the connection.
func (c *Client) Stop() {
	close(c.stopCh)
	c.mu.Lock()
	if c.conn != nil {
		_ = c.conn.Close()
	}
	c.mu.Unlock()
}

// eventFrame is the OUTBOUND frame (daemon → HQ). API Gateway routes on
// `$request.body.action`, and the `event` Lambda then validates the entire body
// as an Envelope (`parseEnvelope(body)`). So the envelope fields are flattened to
// the top level (anonymous embed) and sit alongside `action` — the backend's zod
// schema ignores the extra `action` key.
type eventFrame struct {
	Action string `json:"action"`
	event.Envelope
}

// inMsg is the INBOUND frame (HQ → daemon). The backend posts control frames as
// `{ type: 'control', sessionId, action, payload }` (and event fan-out as
// `{ type: 'event', ... }`, which daemons ignore). We dispatch on `type`.
type inMsg struct {
	Type      string        `json:"type"`
	SessionID string        `json:"sessionId"`
	Action    ControlAction `json:"action"`
	Payload   string        `json:"payload,omitempty"`
}
