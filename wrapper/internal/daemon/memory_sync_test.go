package daemon

import (
	"encoding/json"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/capture"
)

// stubMemorySync replaces the package-level syncMemoriesNow seam with a counting
// stub for the duration of a test, restoring the real one after. It returns a
// getter for how many times the sync ran and the last repoRoot it saw, so a test
// can assert a Stop hook drove exactly one (debounced) reconcile.
func stubMemorySync(t *testing.T) (calls func() int32, lastRepo func() string) {
	t.Helper()
	var n int32
	var mu sync.Mutex
	var last string
	prev := syncMemoriesNow
	syncMemoriesNow = func(repoRoot string) error {
		atomic.AddInt32(&n, 1)
		mu.Lock()
		last = repoRoot
		mu.Unlock()
		return nil
	}
	t.Cleanup(func() { syncMemoriesNow = prev })
	return func() int32 { return atomic.LoadInt32(&n) },
		func() string { mu.Lock(); defer mu.Unlock(); return last }
}

// TestStopTriggersMemorySync proves the Stop hook kicks the debounced memory
// reconcile against the daemon's repoRoot (the #-feature trigger point).
func TestStopTriggersMemorySync(t *testing.T) {
	calls, lastRepo := stubMemorySync(t)

	repo := t.TempDir()
	d, err := New(repo, nil)
	if err != nil {
		t.Fatalf("New daemon: %v", err)
	}

	hook := capture.HookEvent{HookEventName: "Stop", SessionID: "s1"}
	b, _ := json.Marshal(hook)
	d.ingestHook(string(b))

	// The reconcile is debounced (memSyncDebounce) and runs off the hook path, so it
	// has NOT fired synchronously yet.
	if got := calls(); got != 0 {
		t.Fatalf("memory sync must be debounced, not synchronous: ran %d times", got)
	}

	waitForCond(t, func() bool { return calls() >= 1 })
	if got := calls(); got != 1 {
		t.Fatalf("memory sync ran %d times, want exactly 1", got)
	}
	if lastRepo() != repo {
		t.Fatalf("memory sync repoRoot = %q, want %q", lastRepo(), repo)
	}
}

// TestStopBurstCoalescesMemorySync proves rapid back-to-back Stops collapse into a
// SINGLE reconcile (the debounce requirement) rather than one sync per turn.
func TestStopBurstCoalescesMemorySync(t *testing.T) {
	calls, _ := stubMemorySync(t)

	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatalf("New daemon: %v", err)
	}

	hook := capture.HookEvent{HookEventName: "Stop", SessionID: "s1"}
	b, _ := json.Marshal(hook)
	// Five Stops well within the debounce window must coalesce.
	for i := 0; i < 5; i++ {
		d.ingestHook(string(b))
	}

	waitForCond(t, func() bool { return calls() >= 1 })
	// Give any erroneously-stacked timers a chance to (wrongly) fire.
	time.Sleep(memSyncDebounce)
	if got := calls(); got != 1 {
		t.Fatalf("burst of Stops must coalesce into 1 reconcile, got %d", got)
	}
}
