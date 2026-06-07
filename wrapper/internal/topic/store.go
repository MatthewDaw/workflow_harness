package topic

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// Store persists per-session topic state to small JSON files under the claude+
// config dir's `topic` subdir, keyed by tab session id (C6). It is written after
// each fold and loaded on captureLoop start, so a mid-session daemon restart
// resumes the carried topic/cadence/debounce state instead of cold-starting.
//
// It is best-effort: a write/read error never blocks a turn (the daemon logs and
// continues with in-memory state), so the checkpoint is an optimization, not a
// correctness dependency.
type Store struct {
	dir string
	mu  sync.Mutex
}

// NewStore returns a Store rooted at <configDir>/topic, creating the dir. A nil
// Store (NewStore returned an error and the caller passed nil) is safe: Load/Save
// become no-ops, so the gate runs on ephemeral state.
func NewStore(configDir string) (*Store, error) {
	dir := filepath.Join(configDir, "topic")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return &Store{dir: dir}, nil
}

// path is the checkpoint file for a session. The session id is sanitized so a
// foreign/odd id can't escape the topic dir.
func (st *Store) path(sessID string) string {
	return filepath.Join(st.dir, sanitizeID(sessID)+".json")
}

// Load reads a session's checkpoint, returning a zero State (ok=false) when none
// exists yet or it is unreadable/corrupt — a fresh session.
func (st *Store) Load(sessID string) (State, bool) {
	if st == nil {
		return State{}, false
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	b, err := os.ReadFile(st.path(sessID))
	if err != nil {
		return State{}, false
	}
	var s State
	if json.Unmarshal(b, &s) != nil {
		return State{}, false
	}
	return s, true
}

// Save persists a session's state atomically (write-temp-then-rename), so a crash
// mid-write never leaves a truncated checkpoint that Load would discard.
func (st *Store) Save(sessID string, s State) error {
	if st == nil {
		return nil
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	p := st.path(sessID)
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}

// Forget removes a session's checkpoint when it ends, so the topic dir doesn't
// grow without bound. Best-effort.
func (st *Store) Forget(sessID string) {
	if st == nil {
		return
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	_ = os.Remove(st.path(sessID))
}

// sanitizeID maps a session id to a safe filename component (alnum, dash,
// underscore preserved; everything else dashed), so a path separator in a foreign
// resumed id cannot traverse out of the topic dir.
func sanitizeID(id string) string {
	if id == "" {
		return "session"
	}
	var b strings.Builder
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteByte('-')
		}
	}
	return b.String()
}
