package pty

import (
	"runtime"
	"strings"
	"testing"
	"time"
)

// TestSessionPTYEcho actually launches a child process under the PTY and reads
// its output back — proving the pseudo-terminal works at runtime on the host
// OS (ConPTY on Windows, native PTYs on macOS/Linux), not just that it compiles.
func TestSessionPTYEcho(t *testing.T) {
	const marker = "claude-plus-pty-ok"
	spawn := func(repoRoot, sessionID string) CmdSpec {
		if runtime.GOOS == "windows" {
			return CmdSpec{Name: "cmd", Args: []string{"/c", "echo " + marker}}
		}
		return CmdSpec{Name: "sh", Args: []string{"-c", "echo " + marker}}
	}

	s, err := newSession("t1", "t1", "", 80, 24, spawn, false, nil)
	if err != nil {
		t.Fatalf("newSession: %v", err)
	}
	defer s.Close()

	var sb strings.Builder
	done := make(chan struct{})
	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := s.Read(buf)
			if n > 0 {
				sb.WriteString(string(buf[:n]))
			}
			if err != nil {
				break
			}
		}
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
	}

	if !strings.Contains(sb.String(), marker) {
		t.Fatalf("PTY output missing marker %q; got: %q", marker, sb.String())
	}
}
