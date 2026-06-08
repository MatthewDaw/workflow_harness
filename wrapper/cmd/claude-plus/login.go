package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/workflow-harness/claude-plus/internal/daemon"
)

// Default HQ endpoints (the deployed Command HQ). They can be overridden with
// the --api / --ws flags, e.g. for a staging stack or a local emulator.
const (
	defaultAPIBase = "https://l5edwucexb.execute-api.us-east-1.amazonaws.com"
	defaultWSURL   = "wss://fgxq7ezbl1.execute-api.us-east-1.amazonaws.com/prod"
)

// cmdLogin runs the device-code flow against HQ and writes the resulting token
// to ~/.claude-plus/credentials in the format the daemon runtime reads:
//
//	line 1: WS URL          (loadHQConfig)
//	line 2: device token    (loadHQConfig)
//	line 3: REST API base   (loadAPIBase, optional)
//
// The flow mirrors packages/backend/src/auth/device.ts: start -> show the user
// code -> poll until the human approves in HQ -> the first post-approval poll
// mints the signed wrapper token.
func cmdLogin(args []string) error {
	apiBase := defaultAPIBase
	wsURL := defaultWSURL
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--api":
			if i+1 >= len(args) {
				return fmt.Errorf("--api requires a URL")
			}
			i++
			apiBase = args[i]
		case "--ws":
			if i+1 >= len(args) {
				return fmt.Errorf("--ws requires a URL")
			}
			i++
			wsURL = args[i]
		default:
			return fmt.Errorf("unknown login flag %q", args[i])
		}
	}
	apiBase = strings.TrimRight(apiBase, "/")

	client := &http.Client{Timeout: 15 * time.Second}

	// 1. start: obtain the device code (polled) and the user code (shown).
	var start struct {
		DeviceCode string `json:"deviceCode"`
		UserCode   string `json:"userCode"`
		ExpiresAt  int64  `json:"expiresAt"`
	}
	if err := postJSON(client, apiBase+"/device/start", nil, &start); err != nil {
		return fmt.Errorf("start device login: %w", err)
	}
	if start.DeviceCode == "" || start.UserCode == "" {
		return fmt.Errorf("start device login: empty response from HQ")
	}

	fmt.Println("To finish signing in, open Command HQ and approve this code:")
	fmt.Println()
	fmt.Println("    " + start.UserCode)
	fmt.Println()
	fmt.Println("Waiting for approval… (Ctrl-C to cancel)")

	// 2. poll: until the human approves, then claim the minted token. The
	// backend reports `pending` until approval and `token` exactly once after.
	deadline := time.Now().Add(10 * time.Minute)
	if start.ExpiresAt > 0 {
		if exp := time.UnixMilli(start.ExpiresAt); exp.Before(deadline) {
			deadline = exp
		}
	}
	token := ""
	for time.Now().Before(deadline) {
		var poll struct {
			Status string `json:"status"`
			Token  string `json:"token"`
		}
		if err := postJSON(client, apiBase+"/device/poll",
			map[string]string{"deviceCode": start.DeviceCode}, &poll); err != nil {
			return fmt.Errorf("poll device login: %w", err)
		}
		switch poll.Status {
		case "token":
			token = poll.Token
		case "pending":
			time.Sleep(2 * time.Second)
			continue
		case "expired":
			return fmt.Errorf("the login code expired before it was approved — run `claude+ login` again")
		default: // "unknown" or anything else
			return fmt.Errorf("login was not approved (status %q)", poll.Status)
		}
		break
	}
	if token == "" {
		return fmt.Errorf("timed out waiting for approval — run `claude+ login` again")
	}

	// 3. store: write the credentials file the daemon already reads.
	//
	// TODO(security): store the device token in the OS keychain (Keychain on
	// macOS, Credential Manager on Windows, Secret Service on Linux) instead of
	// a plaintext file. For now we persist to ~/.claude-plus/credentials with
	// owner-only permissions, matching the daemon's existing loadHQConfig path.
	if err := writeCredentials(wsURL, token, apiBase); err != nil {
		return fmt.Errorf("save credentials: %w", err)
	}

	fmt.Println()
	fmt.Println("Signed in. Credentials saved to ~/.claude-plus/credentials.")

	// Immediately materialize this repo's enabled skills/agents/MCP with the fresh
	// token, so a single in-session `! claude+ login` both authenticates AND pulls
	// everything down — no second command. SyncSkillsNow re-reads the credentials
	// file we just wrote, so it does not depend on any running daemon's cached
	// token. Best-effort: a sync failure (e.g. run outside a repo) never fails the
	// login itself.
	if repo, rerr := resolveRepoRoot(); rerr == nil {
		if pulled, _, gate, serr := daemon.SyncSkillsNow(repo); serr == nil {
			fmt.Printf("Synced this repo: pulled %d item(s).\n", pulled)
			for _, na := range gate.NeedsAuth() {
				fmt.Printf("  · MCP %q needs interactive auth: %s\n", na.Name, na.Detail)
			}
		}
	}
	fmt.Println("Next: start a fresh claude+ session here so the new /commands load into autocomplete.")
	return nil
}

// writeCredentials persists the HQ connection details in the daemon's expected
// "url\ntoken\napiBase\n" format with owner-only permissions.
func writeCredentials(wsURL, token, apiBase string) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	dir := filepath.Join(home, ".claude-plus")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	body := wsURL + "\n" + token + "\n" + apiBase + "\n"
	return os.WriteFile(filepath.Join(dir, "credentials"), []byte(body), 0o600)
}

// postJSON POSTs an optional JSON body and decodes a JSON response. A nil body
// sends an empty POST. Non-2xx responses become errors.
func postJSON(client *http.Client, url string, body any, dst any) error {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(http.MethodPost, url, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("content-type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("%s", resp.Status)
	}
	if dst == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(dst)
}
