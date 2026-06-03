package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
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
func runGUI() error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	bin, err := findDesktopBinary(self)
	if err != nil {
		return err
	}
	cmd := exec.Command(bin)
	cmd.Dir, _ = os.Getwd()
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return cmd.Start()
}
