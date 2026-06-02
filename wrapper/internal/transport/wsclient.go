package transport

import (
	"net/url"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"github.com/workflow-harness/claude-plus/internal/event"
)

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
}

// NewClient constructs a transport client. buf must be open; onCtrl receives
// control frames routed down from HQ. instanceID identifies this daemon so HQ can
// route steering commands back to it (the `$connect` handler indexes it).
func NewClient(url, token, instanceID string, buf *RingBuffer, onCtrl ControlHandler) *Client {
	return &Client{
		url: url, token: token, instanceID: instanceID, buf: buf, onCtrl: onCtrl,
		sendCh: make(chan event.Envelope, 256),
		stopCh: make(chan struct{}),
		dial:   defaultDial,
	}
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
	c, _, err := websocket.DefaultDialer.Dial(dialURL, nil)
	return c, err
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
			if !c.sleep(backoff) {
				return
			}
			if backoff < 30*time.Second {
				backoff *= 2
			}
			continue
		}
		backoff = time.Second
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

// replay re-sends all unacknowledged envelopes in order, then acks them. This is
// the offline → online catch-up; ordering and seq are preserved by the buffer.
func (c *Client) replay(conn *websocket.Conn) error {
	pending, err := c.buf.Pending()
	if err != nil {
		return err
	}
	for _, env := range pending {
		if err := conn.WriteJSON(eventFrame{Action: "event", Envelope: env}); err != nil {
			return err
		}
	}
	if len(pending) > 0 {
		return c.buf.Ack(len(pending))
	}
	return nil
}

// serve pumps live sends and reads control frames until the connection drops.
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
		case env := <-c.sendCh:
			if err := conn.WriteJSON(eventFrame{Action: "event", Envelope: env}); err != nil {
				return
			}
			_ = c.buf.Ack(1)
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
