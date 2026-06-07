package daemon

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// PersistedSession is the durable, restart-surviving description of one logical
// PTY session. It is the minimum state the daemon needs to resume the SAME
// Claude conversation (claude --resume <TabID>) and keep HQ streaming it as the
// same logical session with no duplicate and no dropped events:
//
//   - TabID            the session id (== Claude conversation id) to --resume.
//   - Name             the current display name / title.
//   - ManualName       true once the user renamed it explicitly (so auto-title
//                      must not clobber it after a restart).
//   - FirstSet         true once the first (auto) name was established.
//   - TitleSet         true once a model-derived title was applied.
//   - TranscriptOffset byte offset already tailed+emitted from the transcript,
//                      so a resumed Tailer skips past it and never re-emits a
//                      line HQ already saw.
//   - NextSeq          the next per-session envelope seq to hand out. A resumed
//                      Seq must start at/above this so new events strictly
//                      exceed every seq HQ already folded.
//
// TranscriptOffset and NextSeq are persisted together as one unit so they always
// describe the same moment.
type PersistedSession struct {
	TabID            string `json:"tabId"`
	Name             string `json:"name"`
	ManualName       bool   `json:"manualName"`
	FirstSet         bool   `json:"firstSet"`
	TitleSet         bool   `json:"titleSet"`
	TranscriptOffset int64  `json:"transcriptOffset"`
	NextSeq          int64  `json:"nextSeq"`
}

// debounceInterval bounds how often the async flusher writes to disk. We never
// fsync per event; the debounce coalesces a burst of MarkDirty calls into at
// most one Save every ~2s. A synchronous FlushNow() on graceful stop captures
// the final state.
const debounceInterval = 2 * time.Second

// SessionsStore persists an ORDERED list of PersistedSession to
// ~/.claude-plus/<repoKey>.sessions.json (the daemon's own state dir, alongside
// the registry record — NOT ~/.claude+). Ordering is the announce order, which
// the resume path replays to recreate tabs in their original positions.
//
// All in-memory state is guarded by mu. Writes go through an atomic temp-file +
// os.Rename so a crash mid-write can never leave a half-written (corrupt) file —
// and Load tolerates a corrupt/missing file by returning an empty store anyway.
type SessionsStore struct {
	path string

	mu       sync.Mutex
	sessions []PersistedSession
	dirty    bool
	closed   bool

	// wake signals the flusher that there is dirty state to write (or that it
	// should re-check after closing). stop terminates the flusher goroutine and
	// done is closed when it has exited.
	wake chan struct{}
	stop chan struct{}
	done chan struct{}
}

// sessionsPath returns the persistence file path for a repo root. It mirrors
// metaPath() but with a .sessions.json suffix so it sits beside the registry
// record and is keyed by the same repoKey.
func sessionsPath(repoRoot string) (string, error) {
	dir, err := baseDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, repoKey(repoRoot)+".sessions.json"), nil
}

// Load reads the persisted store for repoRoot. It is tolerant of a missing file
// (ENOENT) and of a corrupt/truncated file: in either case it returns a valid,
// empty store rather than an error, so a daemon can always start. It returns a
// non-nil error only for unexpected filesystem failures (e.g. resolving the
// home dir). The returned store has its async flusher already running.
func Load(repoRoot string) (*SessionsStore, error) {
	path, err := sessionsPath(repoRoot)
	if err != nil {
		return nil, err
	}
	s := &SessionsStore{
		path: path,
		wake: make(chan struct{}, 1),
		stop: make(chan struct{}),
		done: make(chan struct{}),
	}
	if b, err := os.ReadFile(path); err == nil {
		var loaded []PersistedSession
		if jsonErr := json.Unmarshal(b, &loaded); jsonErr == nil {
			s.sessions = loaded
		}
		// On unmarshal error we intentionally keep an empty list: a corrupt
		// file must never wedge daemon startup.
	}
	// A missing file (os.IsNotExist) is the normal first-run case: empty store.
	go s.flushLoop()
	return s, nil
}

// Sessions returns a copy of the ordered session list. The copy keeps callers
// from mutating internal state without the lock.
func (s *SessionsStore) Sessions() []PersistedSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]PersistedSession, len(s.sessions))
	copy(out, s.sessions)
	return out
}

// Upsert inserts ps if its TabID is new (appended at the end, preserving
// announce order) or replaces the existing entry with the same TabID in place
// (preserving its position). It marks the store dirty so the flusher persists
// it. An empty TabID is ignored.
func (s *SessionsStore) Upsert(ps PersistedSession) {
	if ps.TabID == "" {
		return
	}
	s.mu.Lock()
	replaced := false
	for i := range s.sessions {
		if s.sessions[i].TabID == ps.TabID {
			s.sessions[i] = ps
			replaced = true
			break
		}
	}
	if !replaced {
		s.sessions = append(s.sessions, ps)
	}
	s.markDirtyLocked()
	s.mu.Unlock()
}

// Remove deletes the session with the given TabID, preserving the order of the
// remaining entries. It marks the store dirty. Removing an unknown id is a
// no-op (but still marks dirty harmlessly only if something changed).
func (s *SessionsStore) Remove(tabID string) {
	s.mu.Lock()
	changed := false
	out := s.sessions[:0]
	for _, ps := range s.sessions {
		if ps.TabID == tabID {
			changed = true
			continue
		}
		out = append(out, ps)
	}
	s.sessions = out
	if changed {
		s.markDirtyLocked()
	}
	s.mu.Unlock()
}

// MarkDirty flags the store for an asynchronous (debounced) flush. Callers use
// it after mutating a field through their own path, or simply to ensure the
// latest in-memory state reaches disk within the debounce window.
func (s *SessionsStore) MarkDirty() {
	s.mu.Lock()
	s.markDirtyLocked()
	s.mu.Unlock()
}

// markDirtyLocked sets the dirty flag and nudges the flusher. Caller holds mu.
// The wake channel is buffered (cap 1) so the non-blocking send never stalls a
// holder of the lock even if the flusher is mid-write.
func (s *SessionsStore) markDirtyLocked() {
	s.dirty = true
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// FlushNow synchronously persists the current state if dirty (or always, when
// force-checked) and returns any write error. It is safe to call on graceful
// stop to guarantee the final state is on disk. It clears the dirty flag on a
// successful write.
func (s *SessionsStore) FlushNow() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.saveLocked()
}

// Save is an exported synchronous flush, equivalent to FlushNow. It writes the
// current ordered list atomically.
func (s *SessionsStore) Save() error { return s.FlushNow() }

// saveLocked atomically writes the current session list to disk. Caller holds
// mu. It writes to a temp file in the SAME directory (so os.Rename is atomic on
// the same filesystem) and renames it over the target. On success it clears the
// dirty flag.
func (s *SessionsStore) saveLocked() error {
	b, err := json.MarshalIndent(s.sessions, "", "  ")
	if err != nil {
		return err
	}
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".sessions-*.json.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	// Best-effort cleanup if anything below fails before the rename succeeds.
	defer func() { _ = os.Remove(tmpName) }()
	if _, err := tmp.Write(b); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return err
	}
	s.dirty = false
	return nil
}

// flushLoop is the debounced async flusher goroutine. It coalesces MarkDirty
// nudges and writes at most once per debounceInterval, and performs a final
// flush when stopped if state is still dirty.
func (s *SessionsStore) flushLoop() {
	defer close(s.done)
	ticker := time.NewTicker(debounceInterval)
	defer ticker.Stop()
	for {
		select {
		case <-s.stop:
			s.mu.Lock()
			if s.dirty {
				_ = s.saveLocked()
			}
			s.mu.Unlock()
			return
		case <-s.wake:
			// Debounce: wait for either the tick or stop before writing, so a
			// burst of nudges collapses into one Save.
			select {
			case <-s.stop:
				s.mu.Lock()
				if s.dirty {
					_ = s.saveLocked()
				}
				s.mu.Unlock()
				return
			case <-ticker.C:
				s.mu.Lock()
				if s.dirty {
					_ = s.saveLocked()
				}
				s.mu.Unlock()
			}
		case <-ticker.C:
			s.mu.Lock()
			if s.dirty {
				_ = s.saveLocked()
			}
			s.mu.Unlock()
		}
	}
}

// Stop terminates the flusher goroutine, performing a final synchronous flush
// of any dirty state, and blocks until the goroutine has exited. It is
// idempotent. Close is an alias for Stop.
func (s *SessionsStore) Stop() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	s.mu.Unlock()
	close(s.stop)
	<-s.done
}

// Close terminates the flusher and flushes (alias for Stop).
func (s *SessionsStore) Close() { s.Stop() }
