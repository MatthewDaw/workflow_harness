package daemon

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// withTempHome points HOME and USERPROFILE at a fresh temp dir so the store
// writes under a throwaway ~/.claude-plus and never touches the real one. It
// returns a repo root path to key the store by.
func withTempHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)        // os.UserHomeDir on unix
	t.Setenv("USERPROFILE", home) // os.UserHomeDir on windows
	return filepath.Join(home, "repo")
}

func tabIDs(ss []PersistedSession) []string {
	out := make([]string, len(ss))
	for i, s := range ss {
		out[i] = s.TabID
	}
	return out
}

func TestSessionsStoreRoundTrip(t *testing.T) {
	repo := withTempHome(t)

	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := s.Sessions(); len(got) != 0 {
		t.Fatalf("fresh store should be empty, got %d", len(got))
	}

	want := []PersistedSession{
		{TabID: "a", Name: "Alpha", FirstSet: true, TranscriptOffset: 10, NextSeq: 3},
		{TabID: "b", Name: "Beta", ManualName: true, TitleSet: true, TranscriptOffset: 99, NextSeq: 42},
	}
	for _, ps := range want {
		s.Upsert(ps)
	}
	if err := s.FlushNow(); err != nil {
		t.Fatalf("FlushNow: %v", err)
	}
	s.Stop()

	// Reload from disk in a fresh store and confirm exact equality + order.
	s2, err := Load(repo)
	if err != nil {
		t.Fatalf("reload Load: %v", err)
	}
	defer s2.Stop()
	got := s2.Sessions()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("round-trip mismatch:\n got=%+v\nwant=%+v", got, want)
	}
}

func TestSessionsStoreUpsertOrderAndReplace(t *testing.T) {
	repo := withTempHome(t)
	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	defer s.Stop()

	s.Upsert(PersistedSession{TabID: "x", Name: "first"})
	s.Upsert(PersistedSession{TabID: "y", Name: "second"})
	s.Upsert(PersistedSession{TabID: "z", Name: "third"})

	if got, want := tabIDs(s.Sessions()), []string{"x", "y", "z"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("announce order not preserved: got %v want %v", got, want)
	}

	// Replace the middle entry: it must stay in position and update fields.
	s.Upsert(PersistedSession{TabID: "y", Name: "second-renamed", ManualName: true, NextSeq: 7})

	got := s.Sessions()
	if ids, want := tabIDs(got), []string{"x", "y", "z"}; !reflect.DeepEqual(ids, want) {
		t.Fatalf("replace must preserve position: got %v want %v", ids, want)
	}
	if got[1].Name != "second-renamed" || !got[1].ManualName || got[1].NextSeq != 7 {
		t.Fatalf("replace did not update fields: %+v", got[1])
	}
	if len(got) != 3 {
		t.Fatalf("replace must not grow list, got %d", len(got))
	}

	// Empty TabID is ignored.
	s.Upsert(PersistedSession{TabID: ""})
	if len(s.Sessions()) != 3 {
		t.Fatalf("empty TabID upsert should be a no-op, got %d", len(s.Sessions()))
	}
}

func TestSessionsStoreRemove(t *testing.T) {
	repo := withTempHome(t)
	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	s.Upsert(PersistedSession{TabID: "a"})
	s.Upsert(PersistedSession{TabID: "b"})
	s.Upsert(PersistedSession{TabID: "c"})

	s.Remove("b")
	if got, want := tabIDs(s.Sessions()), []string{"a", "c"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Remove order: got %v want %v", got, want)
	}

	// Removing an unknown id is a no-op.
	s.Remove("missing")
	if got, want := tabIDs(s.Sessions()), []string{"a", "c"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Remove unknown changed list: got %v want %v", got, want)
	}

	if err := s.FlushNow(); err != nil {
		t.Fatalf("FlushNow: %v", err)
	}
	s.Stop()

	s2, err := Load(repo)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	defer s2.Stop()
	if got, want := tabIDs(s2.Sessions()), []string{"a", "c"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Remove not persisted: got %v want %v", got, want)
	}
}

func TestSessionsStoreAtomicOverwrite(t *testing.T) {
	repo := withTempHome(t)
	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	s.Upsert(PersistedSession{TabID: "a", NextSeq: 1})
	if err := s.FlushNow(); err != nil {
		t.Fatalf("first FlushNow: %v", err)
	}

	// Overwrite with new content; the file must reflect the latest state only,
	// with no leftover temp files in the directory.
	s.Upsert(PersistedSession{TabID: "a", NextSeq: 100})
	s.Upsert(PersistedSession{TabID: "d", NextSeq: 5})
	if err := s.FlushNow(); err != nil {
		t.Fatalf("second FlushNow: %v", err)
	}
	s.Stop()

	path, err := sessionsPath(repo)
	if err != nil {
		t.Fatalf("sessionsPath: %v", err)
	}
	dir := filepath.Dir(path)
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	tmpCount := 0
	for _, e := range entries {
		if filepath.Ext(e.Name()) == ".tmp" || (len(e.Name()) >= 4 && e.Name()[len(e.Name())-4:] == ".tmp") {
			tmpCount++
		}
	}
	if tmpCount != 0 {
		t.Fatalf("atomic write left %d temp file(s) behind", tmpCount)
	}

	s2, err := Load(repo)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	defer s2.Stop()
	got := s2.Sessions()
	want := []PersistedSession{
		{TabID: "a", NextSeq: 100},
		{TabID: "d", NextSeq: 5},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("overwrite content mismatch:\n got=%+v\nwant=%+v", got, want)
	}
}

func TestSessionsStoreCorruptFileTolerance(t *testing.T) {
	repo := withTempHome(t)
	path, err := sessionsPath(repo)
	if err != nil {
		t.Fatalf("sessionsPath: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte("{ this is not valid json"), 0o600); err != nil {
		t.Fatalf("write corrupt: %v", err)
	}

	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load on corrupt file should not error, got: %v", err)
	}
	defer s.Stop()
	if got := s.Sessions(); len(got) != 0 {
		t.Fatalf("corrupt file should yield empty store, got %d", len(got))
	}

	// The store must still be usable: upsert + flush recovers a valid file.
	s.Upsert(PersistedSession{TabID: "recovered", NextSeq: 9})
	if err := s.FlushNow(); err != nil {
		t.Fatalf("FlushNow after corrupt recovery: %v", err)
	}

	s2, err := Load(repo)
	if err != nil {
		t.Fatalf("reload after recovery: %v", err)
	}
	defer s2.Stop()
	if got, want := tabIDs(s2.Sessions()), []string{"recovered"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("recovery not persisted: got %v want %v", got, want)
	}
}

func TestSessionsStoreMissingFile(t *testing.T) {
	repo := withTempHome(t)
	// No file written at all — Load must succeed with an empty store and never
	// error on ENOENT.
	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load on missing file should not error, got: %v", err)
	}
	defer s.Stop()
	if got := s.Sessions(); len(got) != 0 {
		t.Fatalf("missing file should yield empty store, got %d", len(got))
	}
}

func TestSessionsStoreDebouncedFlush(t *testing.T) {
	repo := withTempHome(t)
	s, err := Load(repo)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	s.Upsert(PersistedSession{TabID: "async", NextSeq: 1})
	// Stop performs a final synchronous flush of dirty state, so the async
	// path's correctness is observable without sleeping for the debounce.
	s.Stop()

	s2, err := Load(repo)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	defer s2.Stop()
	if got, want := tabIDs(s2.Sessions()), []string{"async"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Stop did not flush dirty state: got %v want %v", got, want)
	}

	// Stop is idempotent.
	s2.Stop()
}
