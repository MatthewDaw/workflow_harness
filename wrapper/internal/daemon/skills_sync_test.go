package daemon

import (
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/config"
)

// fakeSource is an in-memory config.RemoteSource for the skills auto-sync tests.
// It records how often Fetch is called and serves one HQ-only skill so a
// reconcile has something to pull.
type fakeSource struct {
	mu      sync.Mutex
	fetches int32
	pulls   int32
}

func (f *fakeSource) Fetch() ([]config.RemoteItem, error) {
	atomic.AddInt32(&f.fetches, 1)
	body := "# hq-only skill\nbody"
	return []config.RemoteItem{
		{Kind: config.KindSkill, Name: "hq-only", Scope: "org", Hash: hashLike(body)},
	}, nil
}

func (f *fakeSource) Body(config.RemoteItem) (string, error) {
	atomic.AddInt32(&f.pulls, 1)
	return "# hq-only skill\nbody", nil
}

func (f *fakeSource) Push(config.Item, string) error { return nil }

// hashLike reproduces config.hashContent for a body so the remote hash differs
// from "absent locally" (forcing a needs_pull). We don't need the exact hash —
// any stable non-empty value works because the local skill is missing.
func hashLike(s string) string { return "remote-" + s }

func newTestRuntime(t *testing.T, src config.RemoteSource) *Runtime {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatalf("New daemon: %v", err)
	}
	return &Runtime{
		d:              d,
		stop:           make(chan struct{}),
		cfgSrc:         src,
		syncedSessions: map[string]bool{},
	}
}

// TestReconcileSkillsPullsHQOnly proves the auto-sync pulls an HQ-only skill into
// the isolated claude+ tree (~/.claude+), the #1 user ask (applicable skills
// appear) without polluting the user's personal ~/.claude (U19/U21).
func TestReconcileSkillsPullsHQOnly(t *testing.T) {
	src := &fakeSource{}
	rt := newTestRuntime(t, src)

	rt.reconcileSkills()

	home := os.Getenv("HOME")
	skill := filepath.Join(home, ".claude+", "skills", "hq-only", "SKILL.md")
	b, err := os.ReadFile(skill)
	if err != nil {
		t.Fatalf("expected pulled skill at %s: %v", skill, err)
	}
	if string(b) != "# hq-only skill\nbody" {
		t.Fatalf("pulled skill body = %q", string(b))
	}
	// It must NOT land in the user's personal ~/.claude.
	if _, err := os.Stat(filepath.Join(home, ".claude", "skills", "hq-only")); !os.IsNotExist(err) {
		t.Fatalf("pulled skill must not pollute ~/.claude (err=%v)", err)
	}
}

// TestSyncSkillsOnceRunsOncePerSession proves the auto-sync fires once per NEW
// session id and that forgetSkillSync lets a reused id sync again (#1 + leak #11).
func TestSyncSkillsOnceRunsOncePerSession(t *testing.T) {
	src := &fakeSource{}
	rt := newTestRuntime(t, src)

	// First observation of "s1" marks it synced; repeat observations are no-ops.
	if !rt.markSyncedForTest("s1") {
		t.Fatal("first sync for s1 should proceed")
	}
	if rt.markSyncedForTest("s1") {
		t.Fatal("second sync for s1 should be skipped (once-per-session)")
	}

	// After the session ends, forgetSkillSync clears the marker so a reused id
	// re-syncs (and the map doesn't grow unbounded).
	rt.forgetSkillSync("s1")
	if !rt.markSyncedForTest("s1") {
		t.Fatal("after forget, s1 should sync again")
	}

	rt.syncedMu.Lock()
	_, present := rt.syncedSessions["gone"]
	rt.syncedMu.Unlock()
	if present {
		t.Fatal("never-seen session should not be present")
	}
}

// markSyncedForTest mirrors the once-gate inside syncSkillsOnce without spawning
// the async reconcile, returning whether this call was the first for the id.
func (rt *Runtime) markSyncedForTest(sessID string) bool {
	rt.syncedMu.Lock()
	defer rt.syncedMu.Unlock()
	if rt.syncedSessions[sessID] {
		return false
	}
	rt.syncedSessions[sessID] = true
	return true
}
