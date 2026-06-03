// Package daemon implements the per-repo background daemon and the thin attach
// client (the tmux model from KTD2). A daemon is bound to a repo, hosts the PTY
// sessions, and survives client disconnect. Clients attach over a Unix socket
// at ~/.claude-plus/<repo>.sock.
package daemon

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// State describes a daemon's lifecycle state for `claude+ ls`.
type State string

const (
	StateRunning State = "running"
	StateStale   State = "stale" // socket present but daemon not answering
)

// Entry is one row in the daemon registry, surfaced by `claude+ ls`.
type Entry struct {
	Index    int       `json:"index"`
	Repo     string    `json:"repo"`     // absolute repo root path
	RepoName string    `json:"repoName"` // basename, for display
	Host     string    `json:"host"`
	Sessions int       `json:"sessions"`
	State    State     `json:"state"`
	Started  time.Time `json:"started"`
	Sock     string    `json:"sock"`
	PID      int       `json:"pid"`
	// Version is the daemon's ProtocolVersion at the time it registered. It lets
	// a freshly-built client detect an incompatible (older/newer build) daemon
	// from the registry alone, and pick the stale entry out for replacement.
	Version int `json:"version"`
}

// Uptime returns the entry's uptime relative to now.
func (e Entry) Uptime() time.Duration { return time.Since(e.Started) }

// baseDir returns ~/.claude-plus, creating it if needed.
func baseDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(home, ".claude-plus")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return dir, nil
}

// repoKey derives a short, filesystem-safe key for a repo root path. It is used
// to name the socket and registry file so two repos never collide.
func repoKey(repoRoot string) string {
	sum := sha1.Sum([]byte(repoRoot))
	return hex.EncodeToString(sum[:])[:12]
}

// metaPath returns the registry metadata file for a repo root.
func metaPath(repoRoot string) (string, error) {
	dir, err := baseDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, repoKey(repoRoot)+".json"), nil
}

// hostName returns user@host for display, matching the envelope host format.
func hostName() string {
	h, _ := os.Hostname()
	if h == "" {
		h = "localhost"
	}
	if u, err := user.Current(); err == nil && u.Username != "" {
		name := u.Username
		if i := strings.LastIndex(name, "\\"); i >= 0 { // strip Windows DOMAIN\user
			name = name[i+1:]
		}
		return name + "@" + h
	}
	return h
}

// writeMeta persists an Entry's metadata so `ls` can read it without attaching.
func writeMeta(e Entry) error {
	p, err := metaPath(e.Repo)
	if err != nil {
		return err
	}
	b, err := json.MarshalIndent(e, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(p, b, 0o600)
}

// readMeta loads an Entry's metadata for a repo root.
func readMeta(repoRoot string) (Entry, error) {
	p, err := metaPath(repoRoot)
	if err != nil {
		return Entry{}, err
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return Entry{}, err
	}
	var e Entry
	if err := json.Unmarshal(b, &e); err != nil {
		return Entry{}, err
	}
	return e, nil
}

// List enumerates all known daemons (running and stale), sorted by start time
// and assigned a stable index. This powers `claude+ ls` and `--session=N`.
func List() ([]Entry, error) {
	dir, err := baseDir()
	if err != nil {
		return nil, err
	}
	matches, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil {
		return nil, err
	}
	var entries []Entry
	for _, m := range matches {
		b, err := os.ReadFile(m)
		if err != nil {
			continue
		}
		var e Entry
		if err := json.Unmarshal(b, &e); err != nil {
			// Corrupt record: drop it so it can never wedge attach-or-create.
			_ = os.Remove(m)
			continue
		}
		f, ok := probe(e.Sock)
		switch {
		case !ok:
			// Dead/stale: the socket does not answer. Reap the record (and the
			// process, in case a wedged image still holds the port) and drop it
			// from the listing so stale rows never accumulate.
			stopStale(e)
			continue
		case f.Version != ProtocolVersion:
			// Alive but incompatible (an older/newer build lingering across a
			// rebuild). Retire it proactively: this is exactly the daemon that
			// would dead-end the attach handshake. Remove it so the next
			// attach-or-create spawns a fresh, compatible daemon.
			stopStale(e)
			continue
		default:
			e.State = StateRunning
			e.Sessions = f.Sessions
			entries = append(entries, e)
		}
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Started.Before(entries[j].Started)
	})
	for i := range entries {
		entries[i].Index = i
	}
	return entries, nil
}

// Find returns the entry for a repo root if a registry record exists.
func Find(repoRoot string) (Entry, bool, error) {
	e, err := readMeta(repoRoot)
	if err != nil {
		if os.IsNotExist(err) {
			return Entry{}, false, nil
		}
		return Entry{}, false, err
	}
	return e, true, nil
}

// ByIndex returns the entry at index n (as shown by `ls`).
func ByIndex(n int) (Entry, error) {
	entries, err := List()
	if err != nil {
		return Entry{}, err
	}
	if n < 0 || n >= len(entries) {
		return Entry{}, fmt.Errorf("session index %d out of range (have %d)", n, len(entries))
	}
	return entries[n], nil
}

// stopStale forcibly retires a daemon described by e: it kills the daemon's
// process (so a still-running incompatible/stale build can no longer answer the
// recorded port) and removes its registry record. It is best-effort and
// idempotent — a process that is already gone, or a record already deleted, is
// not an error. This is the core of cross-rebuild auto-recovery: the client
// calls it the moment a probe/attach reveals an incompatible daemon, then
// respawns a fresh one.
func stopStale(e Entry) {
	terminatePID(e.PID)
	_ = removeMeta(e.Repo)
}

// Prune removes registry records for daemons that are no longer usable: the
// process is dead, the socket no longer answers, OR the daemon answers with a
// protocol version this build cannot speak. It returns the surviving (live and
// compatible) entries, freshly indexed. `ls` calls List (which prunes inline);
// callers that want an explicit sweep can use Prune.
func Prune() ([]Entry, error) {
	return List()
}

// removeMeta deletes the registry record for a repo root (on clean shutdown).
func removeMeta(repoRoot string) error {
	p, err := metaPath(repoRoot)
	if err != nil {
		return err
	}
	if err := os.Remove(p); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
