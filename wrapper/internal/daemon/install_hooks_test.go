package daemon

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestInstallHooksWritesSettings proves daemon startup's best-effort hook
// install resolves this binary and writes a managed hooks block referencing the
// `__hook` shim into the isolated claude+ config root (~/.claude+/settings.json)
// — the wiring that makes the hook receiver loop live in production. It must
// target ~/.claude+ (not ~/.claude) because claude+ launches Claude with
// CLAUDE_CONFIG_DIR pointed there, so hooks only fire from that root.
func TestInstallHooksWritesSettings(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)        // honoured by os.UserHomeDir on unix
	t.Setenv("USERPROFILE", home) // and on windows

	installHooks()

	b, err := os.ReadFile(filepath.Join(home, ".claude+", "settings.json"))
	if err != nil {
		t.Fatalf("settings.json not written: %v", err)
	}
	if !strings.Contains(string(b), "__hook") {
		t.Fatalf("settings.json missing the __hook shim command: %s", b)
	}
}
