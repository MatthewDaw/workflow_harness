package transport

import (
	"net/http"
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
	url     string
	token   string // device token (Sec-WebSocket auth header)
	buf     *RingBuffer
	onCtrl  ControlHandler

	mu        sync.Mutex
	conn      *websocket.Conn
	connected atomic.Bool

	sendCh chan event.Envelope
	stopCh chan struct{}

	// dial is overridable in tests to inject a fake transport.
	dial func(url, token string) (*websocket.Conn, error)
}

// NewClient constructs a transport client. buf must be open; onCtrl receives
// control frames routed down from HQ.
func NewClient(url, token string, buf *RingBuffer, onCtrl ControlHandler) *Client {
	return &Client{
		url: url, token: token, buf: buf, onCtrl: onCtrl,
		sendCh: make(chan event.Envelope, 256),
		stopCh: make(chan struct{}),
		dial:   defaultDial,
	}
}

func defaultDial(url, token string) (*websocket.Conn, error) {
	h := http.Header{}
	if token != "" {
		h.Set("Authorization", "Bearer "+token)
	}
	c, _, err := websocket.DefaultDialer.Dial(url, h)
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
		conn, err := c.dial(c.url, c.token)
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
		if err := conn.WriteJSON(wireMsg{Action: "event", Envelope: env}); err != nil {
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
			var msg wireMsg
			if err := conn.ReadJSON(&msg); err != nil {
				return
			}
			if msg.Action == "control" && c.onCtrl != nil {
				c.onCtrl(msg.Control)
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
			if err := conn.WriteJSON(wireMsg{Action: "event", Envelope: env}); err != nil {
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

// wireMsg is the frame exchanged with the WebSocket API. Outbound carries an
// event Envelope; inbound carries a ControlFrame. The `action` field matches the
// WebSocket API route keys (event / control) defined by the backend (U6/U7).
type wireMsg struct {
	Action   string         `json:"action"`
	Envelope event.Envelope `json:"envelope,omitempty"`
	Control  ControlFrame   `json:"control,omitempty"`
}
