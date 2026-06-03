package main

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// desktopBinName is the GUI binary's filename for this OS.
func desktopBinName() string {
	if runtime.GOOS == "windows" {
		return "claude-plus-desktop.exe"
	}
	return "claude-plus-desktop"
}

// findDesktopBinary locates the desktop GUI binary: first next to the CLI
// (selfPath's directory), then on PATH. Returns a clear error if absent.
func findDesktopBinary(selfPath string) (string, error) {
	name := desktopBinName()
	sibling := filepath.Join(filepath.Dir(selfPath), name)
	if fi, err := os.Stat(sibling); err == nil && !fi.IsDir() {
		return sibling, nil
	}
	if p, err := exec.LookPath(name); err == nil {
		return p, nil
	}
	return "", fmt.Errorf("desktop app not installed: %q not found next to the CLI or on PATH", name)
}

// runGUI discovers and launches the desktop binary, inheriting cwd so it targets
// the same per-repo daemon. It does not wait — the GUI owns its own lifecycle.
//
// A best-effort, per-repo single-instance guard avoids spawning a second
// desktop window for a repo that already has one: a pidfile under the repo's
// daemon dir records the launched PID; if that process is still alive we print
// a message and return instead of launching again.
func runGUI() error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	bin, err := findDesktopBinary(self)
	if err != nil {
		return err
	}

	repo, _ := os.Getwd()
	if pid, ok := desktopAlreadyRunning(repo); ok {
		fmt.Printf("claude+ desktop is already running for this repo (pid %d).\n", pid)
		return nil
	}

	cmd := exec.Command(bin)
	cmd.Dir = repo
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Start(); err != nil {
		return err
	}
	// Best-effort: record the PID so a later --gui for this repo no-ops. A
	// write failure here never blocks the launch.
	_ = writeGUIPidfile(repo, cmd.Process.Pid)
	return nil
}

// guiPidfilePath returns the per-repo desktop pidfile under ~/.claude-plus,
// keyed by a hash of the repo path so two repos never collide. It mirrors the
// daemon registry's naming (repoKey) without importing its unexported helpers.
func guiPidfilePath(repoRoot string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(home, ".claude-plus")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	sum := sha1.Sum([]byte(repoRoot))
	key := hex.EncodeToString(sum[:])[:12]
	return filepath.Join(dir, key+".gui.pid"), nil
}

// desktopAlreadyRunning reports whether a recorded desktop PID for this repo is
// still alive. A missing/garbage pidfile, or a dead PID, means "not running".
func desktopAlreadyRunning(repoRoot string) (int, bool) {
	path, err := guiPidfilePath(repoRoot)
	if err != nil {
		return 0, false
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, false
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || pid <= 0 {
		return 0, false
	}
	if processAlive(pid) {
		return pid, true
	}
	return 0, false
}

// writeGUIPidfile records the launched desktop PID for this repo.
func writeGUIPidfile(repoRoot string, pid int) error {
	path, err := guiPidfilePath(repoRoot)
	if err != nil {
		return err
	}
	return os.WriteFile(path, []byte(strconv.Itoa(pid)), 0o600)
}
