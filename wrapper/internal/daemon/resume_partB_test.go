package daemon

import (
	"runtime"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/pty"
	"github.com/workflow-harness/claude-plus/internal/transport"
)

// resumeLongSpawn returns a long-running child so a resumed/spawned session stays
// Alive for the duration of a test (cross-platform, like the other lifecycle
// tests). It also records each (repoRoot, sessionID) the mux asked to spawn, so a
// test can prove SpawnResumed reused the persisted id rather than minting a new
// one.
func resumeLongSpawn(seen *[]string) pty.SpawnFunc {
	return func(repoRoot, sessionID string) pty.CmdSpec {
		*seen = append(*seen, sessionID)
		if runtime.GOOS == "windows" {
			return pty.CmdSpec{Name: "ping", Args: []string{"-n", "60", "127.0.0.1"}, Dir: repoRoot}
		}
		return pty.CmdSpec{Name: "sleep", Args: []string{"60"}, Dir: repoRoot}
	}
}

// newResumeDaemon brings up an in-process daemon on a temp HOME (so it never
// touches the developer's real ~/.claude-plus) with the given spawn func, and
// returns it plus the repo root. No HQ creds, so the outbound client is nil.
func newResumeDaemon(t *testing.T, spawn pty.SpawnFunc) (*Daemon, string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("CLAUDE_PLUS_WS_URL", "")
	t.Setenv("CLAUDE_PLUS_TOKEN", "")
	t.Setenv("CLAUDE_PLUS_API_URL", "")
	repo := t.TempDir()
	d, err := New(repo, spawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return d, repo
}

// TestResumePersistenceRoundTripAcrossRestart proves the cross-restart resume
// loop: a session is persisted with a known TranscriptOffset/NextSeq, a NEW
// daemon is constructed from that store (the simulated restart), and we assert
// (a) the session is recreated under the SAME id via SpawnResumed (the spawn func
// is asked for the persisted id, not a fresh UUID), and (b) the resumed Seq for
// that session starts ABOVE both the persisted NextSeq high-water AND above a
// seeded ring-buffer max — so any new envelope strictly exceeds everything HQ
// already saw or will replay.
func TestResumePersistenceRoundTripAcrossRestart(t *testing.T) {
	const sid = "11111111-1111-1111-1111-111111111111"
	const persistedNextSeq = int64(42)
	const persistedOffset = int64(2048)
	const bufferedSeq = int64(50) // un-acked in the ring buffer; replays with this seq

	// Share ONE HOME across the simulated restart so the store path (HOME-keyed via
	// baseDir) is identical before and after — exactly as a real daemon restart on
	// the same machine sees the same ~/.claude-plus.
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("CLAUDE_PLUS_WS_URL", "")
	t.Setenv("CLAUDE_PLUS_TOKEN", "")
	t.Setenv("CLAUDE_PLUS_API_URL", "")
	repo := t.TempDir()

	// --- pre-restart: persist the session as it stood when the old daemon died ---
	store, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	store.Upsert(PersistedSession{
		TabID: sid, Name: "resumed-work", FirstSet: true, TitleSet: true,
		TranscriptOffset: persistedOffset, NextSeq: persistedNextSeq,
	})
	if err := store.FlushNow(); err != nil {
		t.Fatalf("FlushNow: %v", err)
	}
	store.Stop()

	// Seed a ring buffer with an un-acked envelope carrying a HIGH seq for sid: on
	// reconnect it replays with this ORIGINAL seq, so the resumed floor must clear
	// 1+bufferedSeq, which here is ABOVE the persisted NextSeq (proving the buffer
	// term dominates when it is larger).
	bufPath := t.TempDir() + "/outbound.jsonl"
	buf, err := transport.OpenRingBuffer(bufPath)
	if err != nil {
		t.Fatalf("OpenRingBuffer: %v", err)
	}
	if err := buf.Append(event.Envelope{V: 1, Seq: bufferedSeq, Event: event.SessionHeartbeat(sid)}); err != nil {
		t.Fatalf("Append: %v", err)
	}

	// --- restart: a NEW daemon + store + runtime built from the persisted state ---
	var spawnedIDs []string
	d2, err := New(repo, resumeLongSpawn(&spawnedIDs))
	if err != nil {
		t.Fatalf("New(2): %v", err)
	}
	defer d2.Stop()

	store2, err := Load(repo)
	if err != nil {
		t.Fatalf("Load(2): %v", err)
	}
	defer store2.Stop()
	got := store2.Sessions()
	if len(got) != 1 || got[0].TabID != sid {
		t.Fatalf("store did not survive the restart: %+v", got)
	}

	// Recreate the session under the SAME id (what RunDaemon does on start).
	s, err := d2.Mux().SpawnResumed(sid, got[0].Name)
	if err != nil {
		t.Fatalf("SpawnResumed: %v", err)
	}
	s.SeedNaming(got[0].Name, got[0].FirstSet, got[0].TitleSet, got[0].ManualName)

	// (a) SpawnResumed asked the spawn func for the PERSISTED id, not a fresh UUID.
	if len(spawnedIDs) != 1 || spawnedIDs[0] != sid {
		t.Fatalf("SpawnResumed launched with %v, want exactly [%s] (same id)", spawnedIDs, sid)
	}
	if s.ID != sid {
		t.Fatalf("resumed session id = %q, want %q", s.ID, sid)
	}

	// Build the runtime from the store and reconcile the seq floor against the
	// buffer — exactly what StartRuntimeWithStore does on the HQ path.
	rt := StartRuntimeWithStore(d2, "resume@test", store2)
	defer rt.Stop()
	rt.reconcileResumeSeqs(buf)

	// (b) The resumed next seq must exceed BOTH the persisted high-water AND the
	// buffered max (1+bufferedSeq).
	next := rt.seq.Peek(sid)
	if next < persistedNextSeq {
		t.Fatalf("resumed next seq %d is below persisted NextSeq %d", next, persistedNextSeq)
	}
	if next <= bufferedSeq {
		t.Fatalf("resumed next seq %d does not exceed buffered seq %d (replayed envelope would collide)", next, bufferedSeq)
	}
	if want := bufferedSeq + 1; next != want {
		t.Fatalf("resumed next seq = %d, want %d (= max(persistedNextSeq=%d, 1+bufferedSeq=%d))",
			next, want, persistedNextSeq, want)
	}
}

// TestResumeSeqReconciliationBeatsBufferedHighWater isolates the
// correctness-critical buffer term: a single un-acked envelope with a very high
// seq must lift the resumed session's NEXT emitted seq strictly above it, so the
// first new event after a restart cannot collide with the about-to-be-replayed
// envelope (HQ dedupes by (sessionId, seq) and would otherwise drop the new one).
func TestResumeSeqReconciliationBeatsBufferedHighWater(t *testing.T) {
	const sid = "22222222-2222-2222-2222-222222222222"
	const bufferedSeq = int64(999)

	d, repo := newResumeDaemon(t, partALongSpawn)
	defer d.Stop()

	// Persist a low NextSeq so the BUFFER term is the one that must dominate.
	store, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	defer store.Stop()
	store.Upsert(PersistedSession{TabID: sid, Name: "x", NextSeq: 3})

	bufPath := t.TempDir() + "/outbound.jsonl"
	buf, err := transport.OpenRingBuffer(bufPath)
	if err != nil {
		t.Fatalf("OpenRingBuffer: %v", err)
	}
	if err := buf.Append(event.Envelope{V: 1, Seq: bufferedSeq, Event: event.UserMsgText(sid, 1, "still pending")}); err != nil {
		t.Fatalf("Append: %v", err)
	}

	rt := StartRuntimeWithStore(d, "resume@test", store)
	defer rt.Stop()
	rt.reconcileResumeSeqs(buf)

	// The NEXT seq Next() would hand out must be strictly greater than the buffered
	// one, and Next() must actually produce a value above it.
	if got := rt.seq.Peek(sid); got <= bufferedSeq {
		t.Fatalf("Peek after reconcile = %d, want > %d", got, bufferedSeq)
	}
	if emitted := rt.seq.Next(sid); emitted <= bufferedSeq {
		t.Fatalf("first emitted seq after resume = %d, want > buffered %d (would be dropped as stale by HQ)", emitted, bufferedSeq)
	}
}

// TestUserKillRemovesFromStoreRestartDoesNot proves the continuity rule: a
// user-intent end (FrameKill / control kill) REMOVES the session from the resume
// store so it is not --resume-d again, while a plain departure (a restart/crash,
// or any non-user end) PRESERVES it. We exercise the runtime's own bookkeeping
// directly (markUserKilled + removeFromStore vs. a non-killed left-mux removal).
func TestUserKillRemovesFromStoreRestartDoesNot(t *testing.T) {
	d, repo := newResumeDaemon(t, partALongSpawn)
	defer d.Stop()

	store, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	defer store.Stop()

	const killed = "33333333-3333-3333-3333-333333333333"
	const preserved = "44444444-4444-4444-4444-444444444444"
	store.Upsert(PersistedSession{TabID: killed, Name: "doomed", NextSeq: 1})
	store.Upsert(PersistedSession{TabID: preserved, Name: "survivor", NextSeq: 1})

	rt := StartRuntimeWithStore(d, "resume@test", store)
	defer rt.Stop()

	// User kill of `killed`: this is what the FrameKill hook + recv.Terminated do.
	rt.markUserKilled(killed)
	rt.removeFromStore(killed)

	// `preserved` left for a NON-user reason: the store must keep it (so the next
	// daemon start resumes it). isUserKilled gates removal, and it is false here.
	if rt.isUserKilled(preserved) {
		t.Fatalf("%s was never user-killed but isUserKilled reported true", preserved)
	}

	if err := store.FlushNow(); err != nil {
		t.Fatalf("FlushNow: %v", err)
	}

	// Re-load the store from disk (the simulated next-start view) and assert the
	// killed session is gone while the preserved one remains.
	reloaded, err := Load(repo)
	if err != nil {
		t.Fatalf("Load(reloaded): %v", err)
	}
	defer reloaded.Stop()
	got := reloaded.Sessions()
	if len(got) != 1 {
		t.Fatalf("after user-kill the store has %d sessions, want 1: %+v", len(got), got)
	}
	if got[0].TabID != preserved {
		t.Fatalf("surviving session = %q, want the non-killed %q", got[0].TabID, preserved)
	}
}

// TestKillHookWiredToStoreRemoval proves the END-TO-END user-kill path through
// the live runtime wiring: StartRuntimeWithStore installs the daemon kill hook,
// so d.notifyKill(id) (what FrameKill calls) removes the session from the store.
func TestKillHookWiredToStoreRemoval(t *testing.T) {
	d, repo := newResumeDaemon(t, partALongSpawn)
	defer d.Stop()

	store, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	defer store.Stop()

	const sid = "55555555-5555-5555-5555-555555555555"
	store.Upsert(PersistedSession{TabID: sid, Name: "kill-me", NextSeq: 1})

	rt := StartRuntimeWithStore(d, "resume@test", store)
	defer rt.Stop()

	// Drive the exact path FrameKill uses (daemon.go's case FrameKill calls this
	// before mux.Kill). The runtime's installed kill hook must remove sid.
	d.notifyKill(sid)

	if err := store.FlushNow(); err != nil {
		t.Fatalf("FlushNow: %v", err)
	}
	if !rt.isUserKilled(sid) {
		t.Fatalf("notifyKill did not record user-intent end for %s", sid)
	}
	if got := store.Sessions(); len(got) != 0 {
		t.Fatalf("kill hook did not remove %s from the store: %+v", sid, got)
	}
}

// TestFreshAnnounceUpsertsStore proves a brand-new (non-resumed) session is
// persisted on first announce, so it would survive a daemon restart. It uses the
// live capture loop (StartRuntimeWithStore + a real Spawn) and waits for the
// store to learn the id.
func TestFreshAnnounceUpsertsStore(t *testing.T) {
	d, repo := newResumeDaemon(t, partALongSpawn)
	defer d.Stop()

	store, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	defer store.Stop()

	rt := StartRuntimeWithStore(d, "resume@test", store)
	defer rt.Stop()

	s, err := d.Mux().Spawn("brand-new")
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	sid := s.ID

	// The capture loop announces on its 1s tick and upserts the store.
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		for _, ps := range store.Sessions() {
			if ps.TabID == sid {
				return // PASS: fresh session persisted for cross-restart resume
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("fresh session %q was never upserted into the resume store", sid)
}
