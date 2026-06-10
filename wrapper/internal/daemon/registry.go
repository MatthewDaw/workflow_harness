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
	// Dangerous records whether the daemon was started in dangerous mode
	// (CLAUDE_PLUS_DANGEROUS set, propagated from
	// `claude+ --dangerously-skip-permissions`), in which case every claude child
	// it spawns runs with --dangerously-skip-permissions. A launch that REQUESTS
	// dangerous mode must not reuse a daemon recorded with Dangerous=false — its
	// children would silently lack the flag. See EnsureDaemon.
	Dangerous bool `json:"dangerous,omitempty"`
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
	// Kill ONLY a process we can positively confirm is the claude+ daemon for this
	// record — never the recorded PID blindly. A messy exit (crash, hard kill,
	// terminal close) skips the daemon's cleanup defer, so the record survives with
	// a PID that is now DEAD, and the OS (Windows especially) recycles that PID fast
	// to an unrelated live process. Because terminatePID is a TREE kill on Windows
	// (taskkill /F /T), terminating a recycled PID would take down an innocent
	// process AND its whole child tree — which is exactly the "a claude+ daemon dies
	// for no reason" failure: a stale record's recycled PID lands on another repo's
	// live daemon (or its claude session) and tree-kills it.
	//
	// So we gate every kill on alive(e.Sock): only when the recorded socket still
	// answers our ping/pong is there a real claude+ daemon to retire. A live daemon
	// rewrites its PID into this record every refresh tick, so when it answers, the
	// listener on its port is the authoritative current PID — kill that (which also
	// covers the case where the REAL daemon kept listening under a different PID than
	// the stale record names). Never kill ourselves: the attaching client (or a test
	// runner hosting an in-process fake daemon) is the current process. When the
	// socket is dead there is nothing safe to kill — a fresh daemon binds a new port
	// and overwrites this record anyway — so we just drop the stale record.
	if alive(e.Sock) {
		if lp := listenerPID(e.Sock); lp > 0 && lp != os.Getpid() {
			terminatePID(lp)
		}
	}
	_ = removeMeta(e.Repo)
}

// ForceReset tears down ALL daemon state for repoRoot so a launch can always
// succeed no matter how messy the previous exit was. It retires the recorded
// daemon (by PID, and by port if a claude+ daemon still holds it), removes the
// record, and waits — bounded — for the port to stop answering so a respawn can
// never race a dying daemon's last refresh write. Safe to call when no daemon
// exists. This is the backstop the attach retry loop uses between attempts.
func ForceReset(repoRoot string) {
	if e, ok, err := Find(repoRoot); err == nil && ok {
		stopStale(e)
		deadline := time.Now().Add(2 * time.Second)
		for time.Now().Before(deadline) && alive(e.Sock) {
			time.Sleep(50 * time.Millisecond)
		}
	}
	_ = removeMeta(repoRoot)
}

// ResetAll force-retires every known daemon and clears the registry, returning
// the number of records cleared. Cross-restart session-resume pointers
// (*.sessions.json) are preserved so a reset never loses conversation continuity.
// It backs `claude+ reset`, the manual escape hatch that guarantees a clean
// restart even if a daemon is wedged in a way the automatic recovery missed.
func ResetAll() int {
	dir, err := baseDir()
	if err != nil {
		return 0
	}
	matches, _ := filepath.Glob(filepath.Join(dir, "*.json"))
	cleared := 0
	for _, m := range matches {
		if strings.HasSuffix(m, ".sessions.json") {
			continue // preserve cross-restart resume pointers
		}
		if b, rerr := os.ReadFile(m); rerr == nil {
			var e Entry
			if json.Unmarshal(b, &e) == nil {
				// Same recycled-PID hazard as stopStale: never tree-kill the recorded
				// PID blindly. Only retire a confirmed-live claude+ daemon by the PID
				// actually listening on its socket.
				if alive(e.Sock) {
					if lp := listenerPID(e.Sock); lp > 0 && lp != os.Getpid() {
						terminatePID(lp)
					}
				}
			}
		}
		_ = os.Remove(m)
		cleared++
	}
	return cleared
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
