// Package diag is a tiny, dependency-free crash/diagnostic logger shared across
// claude+ packages. Detached daemons and background goroutines have no visible
// stderr, so panics there would otherwise vanish; this records them to
// ~/.claude-plus/crash.log for after-the-fact diagnosis.
package diag

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime/debug"
	"sync"
	"time"
)

var mu sync.Mutex

func logPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(os.TempDir(), "claude-plus-crash.log")
	}
	dir := filepath.Join(home, ".claude-plus")
	_ = os.MkdirAll(dir, 0o700)
	return filepath.Join(dir, "crash.log")
}

func write(line string) {
	mu.Lock()
	defer mu.Unlock()
	f, err := os.OpenFile(logPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(line)
}

// LogPanic records a recovered panic with its stack.
func LogPanic(where string, r interface{}, stack []byte) {
	write(fmt.Sprintf("\n=== panic in %s @ %s (pid %d) ===\n%v\n%s\n",
		where, stamp(), os.Getpid(), r, stack))
}

// Logf records a one-line diagnostic entry.
func Logf(format string, args ...interface{}) {
	write(stamp() + " " + fmt.Sprintf(format, args...) + "\n")
}

// Recover logs a panic in a background goroutine and swallows it so a single
// bad event doesn't tear down the whole process. Use as `defer diag.Recover(name)`.
func Recover(where string) {
	if r := recover(); r != nil {
		LogPanic(where, r, debug.Stack())
	}
}

func stamp() string { return time.Now().Format("2006-01-02 15:04:05.000") }
