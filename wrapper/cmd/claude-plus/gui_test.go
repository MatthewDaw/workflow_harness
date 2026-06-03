package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestFindDesktopBinaryPrefersSibling(t *testing.T) {
	dir := t.TempDir()
	name := "claude-plus-desktop"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	sibling := filepath.Join(dir, name)
	if err := os.WriteFile(sibling, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	self := filepath.Join(dir, "claude-plus")
	got, err := findDesktopBinary(self)
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if got != sibling {
		t.Fatalf("got %q, want %q", got, sibling)
	}
}

func TestFindDesktopBinaryMissingIsClearError(t *testing.T) {
	self := filepath.Join(t.TempDir(), "claude-plus")
	_, err := findDesktopBinary(self)
	if err == nil {
		t.Fatal("expected error when desktop binary absent")
	}
}
