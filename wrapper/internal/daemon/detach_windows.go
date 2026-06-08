//go:build windows

package daemon

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// detachAttr puts the daemon child in its own process group on Windows so it is
// not signaled when the launching console closes. Note: Unix-domain sockets are
// supported on modern Windows 10+; this build is provided for completeness, but
// the primary distribution targets are darwin/linux (see .goreleaser.yaml).
func detachAttr(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP,
	}
}

// resolveExe makes the daemon self-spawn path loadable by the Windows process
// creator. Git Bash happily runs an extension-less PE named `claude+`, but then
// os.Executable() reports that extension-less path and exec.Command cannot launch
// it ("executable file not found in %PATH%"), so daemon self-spawn dies. When the
// reported path lacks a Windows executable extension, prefer the adjacent
// `<self>.exe` if it exists; otherwise fall back to appending `.exe` (the name the
// loader will actually try). A path that already has an extension is returned
// unchanged.
func resolveExe(self string) string {
	if ext := strings.ToLower(filepath.Ext(self)); ext == ".exe" || ext == ".com" || ext == ".bat" || ext == ".cmd" {
		return self
	}
	if withExe := self + ".exe"; fileExists(withExe) {
		return withExe
	}
	return self + ".exe"
}

func fileExists(p string) bool {
	info, err := os.Stat(p)
	return err == nil && !info.IsDir()
}

// terminatePID forcibly stops the daemon process AND its child tree on Windows.
// There is no signal/process-group model here, and a plain TerminateProcess on
// the daemon pid leaves its `claude` PTY child (and that child's descendants)
// orphaned — they keep holding their session id + transcript, which then races
// the fresh daemon's resume/spawn. So we kill the whole tree with `taskkill /T`,
// mirroring the unix build's negative-pid group kill. A missing process (already
// dead) is not an error. Fall back to a direct kill if taskkill is unavailable.
func terminatePID(pid int) {
	if pid <= 0 {
		return
	}
	// /T kills the process and its descendants; /F forces it. CombinedOutput is
	// ignored — a non-zero exit (e.g. the process already gone) is best-effort.
	if err := exec.Command("taskkill", "/F", "/T", "/PID", strconv.Itoa(pid)).Run(); err == nil {
		return
	}
	if p, err := os.FindProcess(pid); err == nil {
		_ = p.Kill()
	}
}

// processAlive reports whether a PID names a live process. On Windows
// os.FindProcess always succeeds, so we open the handle and check the exit code:
// a still-running process reports STILL_ACTIVE (259).
func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	// Signal(0) on Windows reports an error once the process has exited.
	return p.Signal(syscall.Signal(0)) == nil
}
