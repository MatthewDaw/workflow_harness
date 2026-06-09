// Command claude-plus is the claude+ CLI: a thin attach client over a per-repo
// background daemon (the tmux model, KTD2).
//
// Usage:
//
//	claude+              attach-or-create the daemon for the current repo
//	claude+ ls           list running daemons (index, repo, host, sessions, state, uptime)
//	claude+ reset        force-retire all daemons + clear the registry (recover a wedged state)
//	claude+ login        device-code sign-in to Command HQ (writes credentials)
//	claude+ --session=N  attach to the daemon at registry index N
//	claude+ stop=N       stop the daemon at registry index N (also accepts `stop N`)
//	claude+ --version    print the version
//
// Hidden verbs used internally:
//
//	claude+ __daemon <repoRoot>   run the detached per-repo daemon (re-exec target)
//	claude+ __hook                forward a Claude Code hook event to the daemon
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/daemon"
)

// version is overridden at build time via -ldflags (goreleaser).
var version = "dev"

func main() {
	// Hidden internal verbs are dispatched before flag parsing. A verb may carry
	// an inline `=value` (e.g. `stop=2`) for parity with the `--session=N` flag;
	// split it off so the switch matches the bare verb and the value is forwarded.
	if len(os.Args) >= 2 {
		verb := os.Args[1]
		inlineVal := ""
		if i := strings.IndexByte(verb, '='); i >= 0 {
			verb, inlineVal = verb[:i], verb[i+1:]
		}
		switch verb {
		case "__daemon":
			runDaemon(os.Args[2:])
			return
		case "__hook":
			runHook()
			return
		case "ls":
			if err := cmdLs(); err != nil {
				fail(err)
			}
			return
		case "login":
			if err := cmdLogin(os.Args[2:]); err != nil {
				fail(err)
			}
			return
		case "sync", "sync-skills":
			// `sync` is the canonical verb: it reconciles EVERYTHING the project
			// reads — skills, agents, AND MCP servers — from both the connected
			// repo's own `.claude` and the Command HQ org catalog (the same engine
			// the in-session `/hq-sync` skill drives). `sync-skills` is a kept-for-
			// back-compat alias (its name undersold what it always did).
			if err := cmdSync(); err != nil {
				fail(err)
			}
			return
		case "run-workflow":
			// Standalone headless executor: run a workflow DAG (catalog agents)
			// wave by wave, reporting live node status to HQ. Reuses the same
			// repoRoot/creds resolution as `sync`; never touches the PTY mux.
			if err := cmdRunWorkflow(os.Args[2:]); err != nil {
				fail(err)
			}
			return
		case "reset":
			if err := cmdReset(); err != nil {
				fail(err)
			}
			return
		case "stop":
			// `stop=N` (inline) and `stop N` (positional) are equivalent; an inline
			// value takes the lead slot so cmdStop sees it as the index argument.
			stopArgs := os.Args[2:]
			if inlineVal != "" {
				stopArgs = append([]string{inlineVal}, stopArgs...)
			}
			if err := cmdStop(stopArgs); err != nil {
				fail(err)
			}
			return
		}
	}

	var sessionN int
	var showVersion bool
	var gui bool
	var dangerous bool
	fs := flag.NewFlagSet("claude+", flag.ContinueOnError)
	fs.IntVar(&sessionN, "session", -1, "attach to the daemon at registry index N")
	fs.BoolVar(&showVersion, "version", false, "print version and exit")
	fs.BoolVar(&gui, "gui", false, "launch the desktop GUI for this repo")
	fs.BoolVar(&dangerous, "dangerously-skip-permissions", false,
		"start this repo's daemon in dangerous mode: every claude session it spawns runs with --dangerously-skip-permissions")
	if err := fs.Parse(os.Args[1:]); err != nil {
		os.Exit(2)
	}

	if showVersion {
		fmt.Println("claude+", version)
		return
	}

	// Dangerous mode is carried to the (possibly detached) daemon and the GUI via
	// the environment: spawnDaemon and runGUI both inherit os.Environ(), and
	// pty.DefaultSpawn honors CLAUDE_PLUS_DANGEROUS on every child. Set it before
	// any EnsureDaemon/runGUI below. The daemon is per-repo and shared, so the
	// mode is fixed when the daemon first starts for a repo.
	if dangerous {
		_ = os.Setenv("CLAUDE_PLUS_DANGEROUS", "1")
	}

	if gui {
		if err := runGUI(); err != nil {
			fail(err)
		}
		return
	}

	if sessionN >= 0 {
		if err := cmdAttachIndex(sessionN); err != nil {
			fail(err)
		}
		return
	}

	if err := cmdAttachOrCreate(); err != nil {
		fail(err)
	}
}

// cmdLs prints the running daemons in a stable, indexed table.
func cmdLs() error {
	entries, err := daemon.List()
	if err != nil {
		return err
	}
	if len(entries) == 0 {
		fmt.Println("no running claude+ daemons")
		return nil
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 2, 2, ' ', 0)
	fmt.Fprintln(w, "IDX\tREPO\tHOST\tSESSIONS\tSTATE\tUPTIME")
	for _, e := range entries {
		fmt.Fprintf(w, "%d\t%s\t%s\t%d\t%s\t%s\n",
			e.Index, e.RepoName, e.Host, e.Sessions, e.State, fmtUptime(e.Uptime()))
	}
	return w.Flush()
}

// cmdAttachOrCreate resolves the repo for cwd and starts a claude+ session,
// TAKING OVER the repo. There is only ever ONE claude+ session per repo: a launch
// terminates any daemon already running for this repo and starts a fresh one, so a
// wedged / incompatible / old-build daemon (e.g. the "protocol v0" case after a
// rebuild) can never block — or be silently reattached by — a new launch. The
// Claude conversation still comes back: ForceReset preserves the cross-restart
// resume pointers, so the fresh daemon resumes the prior conversation.
//
// Implementation: ForceReset up front retires the prior daemon (kills the PID
// listening on its recorded port, clears the record, waits for the socket to die)
// so EnsureDaemon always spawns a fresh daemon from the CURRENT binary. The retry
// loop repeats the teardown if a spawn races a dying daemon's last refresh write.
func cmdAttachOrCreate() error {
	repo, err := resolveRepoRoot()
	if err != nil {
		return err
	}
	// Single session per repo: terminate any existing daemon for this repo before
	// starting, so the launch lands on a fresh daemon from this binary (not a stale
	// or incompatible one). The resume pointers survive, so the conversation does.
	daemon.ForceReset(repo)

	const attempts = 3
	var lastErr error
	for attempt := 0; attempt < attempts; attempt++ {
		if _, err := daemon.EnsureDaemon(repo); err != nil {
			lastErr = fmt.Errorf("start daemon: %w", err)
			daemon.ForceReset(repo)
			continue
		}
		c, err := daemon.Dial(repo)
		if err == nil {
			return runShell(c, filepath.Base(repo), config.ProjectIDFor(repo))
		}
		lastErr = fmt.Errorf("attach: %w", err)
		// Any attach failure — incompatible/garbage daemon, a clobber race, a hung
		// listener — gets a total teardown so the next attempt starts clean.
		daemon.ForceReset(repo)
	}
	return fmt.Errorf("%w (gave up after %d attempts; run `claude+ reset` to clear all daemon state)", lastErr, attempts)
}

// cmdReset is the manual escape hatch: it force-retires every known daemon and
// clears the registry so `claude+` can always start fresh, even if a daemon got
// wedged in a way the automatic per-launch recovery did not catch. Session-resume
// pointers are preserved, so conversations still come back on the next launch.
func cmdReset() error {
	n := daemon.ResetAll()
	fmt.Printf("claude+ reset: cleared %d daemon record(s); next launch starts fresh\n", n)
	return nil
}

// cmdStop stops ONE running session by its `claude+ ls` index: it force-retires
// that folder's daemon — killing its process tree and clearing its record — while
// PRESERVING the session-resume pointers, so the conversations come back on the
// next `claude+` launch in that folder. The targeted counterpart to `claude+ reset`
// (which clears every daemon). The index is the IDX column from `claude+ ls`.
func cmdStop(args []string) error {
	if len(args) < 1 {
		return fmt.Errorf("usage: claude+ stop=<index>   (the IDX column from `claude+ ls`)")
	}
	n, err := strconv.Atoi(strings.TrimSpace(args[0]))
	if err != nil {
		return fmt.Errorf("invalid index %q — pass a number from `claude+ ls`", args[0])
	}
	e, err := daemon.ByIndex(n)
	if err != nil {
		return err
	}
	daemon.ForceReset(e.Repo)
	fmt.Printf("stopped session %d (%s); its conversations resume on the next `claude+` in that folder\n",
		n, e.RepoName)
	return nil
}

// cmdAttachIndex attaches to the daemon at the given `ls` index.
func cmdAttachIndex(n int) error {
	label, hqProject := "", ""
	if e, err := daemon.ByIndex(n); err == nil {
		label = e.RepoName
		hqProject = config.ProjectIDFor(e.Repo)
	}
	c, err := daemon.DialIndex(n)
	if err != nil {
		return err
	}
	return runShell(c, label, hqProject)
}

// cmdSync (`claude+ sync`, alias `sync-skills`) runs a one-shot reconcile of the
// project's EVERYTHING — skills, agents, AND MCP servers — into the isolated
// ~/.claude+ registry, from both the connected repo's own `.claude` and HQ's
// effective enabled set (bundle membership expanded live, so it self-heals a stale
// snapshot). It is the on-demand counterpart to the per-session auto-sync and the
// engine the `/hq-sync` skill drives. Pulled (HQ-only) items land in ~/.claude+,
// never the user's ~/.claude. After reconcile it runs the verification gate
// (U-Verify-Gate): a partial install returns a non-nil error so this command EXITS
// NON-ZERO (fail loudly). MCP servers that materialized but need an interactive
// login are printed (they do not fail the gate) so the user knows what to run.
func cmdSync() error {
	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	// Resolve and SHOW the HQ project id + the exact config root being written. The
	// root slug is keyed on the local repo PATH while the project id is keyed on the
	// git remote — derived independently — so two checkouts of one repo get two
	// roots, and skills enabled on a divergent/duplicate project record land on a
	// record this checkout never reads. Printing both makes that divergence visible
	// instead of surfacing as a silent "pulled 0".
	projectID := config.ProjectIDFor(cwd)
	root, _ := config.ProjectConfigDir(cwd)
	pulled, pushed, gate, err := daemon.SyncSkillsNow(cwd)
	// Surface needs-auth servers regardless of gate pass/fail — they are actionable
	// and are not the reason for any failure.
	for _, na := range gate.NeedsAuth() {
		fmt.Printf("mcp %q needs interactive auth: %s\n", na.Name, na.Detail)
	}
	if err != nil {
		// The gate's error names every missing/invalid item; return it so the process
		// exits non-zero on a partial install.
		return err
	}
	// An empty effective set on a connected project is almost always a wrong/duplicate
	// project record or a divergent root — not a real "nothing enabled" (every
	// connected project carries at least command-hq-starter). Call it out so the user
	// re-checks the binding instead of trusting a clean-looking sync.
	if len(gate.Items) == 0 {
		fmt.Printf("warning: project %q has no enabled skills/agents/mcp servers — "+
			"if you expected some, this checkout may be bound to a different or duplicate "+
			"project record (config root %s)\n", projectID, root)
	}
	fmt.Printf("synced (skills + agents + mcp) for project %q: pulled %d, pushed %d → %s; verify: %s\n",
		projectID, pulled, pushed, root, gate.Summary())
	return nil
}

// runDaemon is the detached daemon entrypoint (`claude+ __daemon <repoRoot>`).
func runDaemon(args []string) {
	if len(args) < 1 {
		fail(fmt.Errorf("__daemon requires a repo root"))
	}
	if err := daemon.RunDaemon(args[0]); err != nil {
		fail(err)
	}
}

// runHook forwards a Claude Code hook event (read as JSON on stdin) to the
// repo's daemon socket. Wired by the capture layer's installed settings.json.
// It always exits 0 so it never blocks a Claude Code turn: a missing daemon,
// unreadable stdin, or a delivery error is swallowed (the transcript tailer
// remains the authoritative event source).
func runHook() {
	// Skip hook forwarding for the internal headless title-generation call
	// (tagged by internal/title via CLAUDE_PLUS_TITLE). That short-lived
	// `claude -p` is not a real session and must not surface in the Stream / HQ.
	if os.Getenv("CLAUDE_PLUS_TITLE") != "" {
		return
	}
	// Likewise skip the internal headless topic-focus judge call (tagged by
	// internal/judge via CLAUDE_PLUS_JUDGE). Without this the judge's own hooks
	// re-enter the daemon — a phantom session plus a Stop→judge→Stop recursion.
	if os.Getenv("CLAUDE_PLUS_JUDGE") != "" {
		return
	}
	// Likewise skip the internal headless workflow-executor node runs (tagged by
	// internal/workflow via CLAUDE_PLUS_WORKFLOW). Each `run-workflow` node is a
	// short-lived `claude -p` agent run, not a real session, and must not surface
	// as a phantom session in the Stream / HQ.
	if os.Getenv("CLAUDE_PLUS_WORKFLOW") != "" {
		return
	}
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, 1<<20))
	if err != nil || len(raw) == 0 {
		return
	}
	// Tag the payload with the pinned launch session id (the tab the daemon keys
	// on). After an in-session /resume, Claude's live session_id diverges from the
	// id we launched with, so without this the daemon can't map the hook back to
	// its tab — auto-naming and status routing would silently miss and the tab
	// would stay on the "session" placeholder. The hook shim inherits
	// CLAUDE_PLUS_SESSION from the PTY launch (see pty.DefaultSpawn).
	raw = tagPinnedSession(raw)
	repo, err := resolveRepoRoot()
	if err != nil {
		return
	}
	_ = daemon.SendHook(repo, raw)
}

// tagPinnedSession injects the child's CLAUDE_PLUS_SESSION (the pinned tab id)
// into the hook JSON as "claude_plus_session". It is a no-op when the env is
// unset or the payload is not a JSON object, returning the raw bytes unchanged
// so an unexpected shape is still forwarded verbatim.
func tagPinnedSession(raw []byte) []byte {
	pinned := os.Getenv("CLAUDE_PLUS_SESSION")
	if pinned == "" {
		return raw
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		return raw
	}
	b, err := json.Marshal(pinned)
	if err != nil {
		return raw
	}
	m["claude_plus_session"] = b
	out, err := json.Marshal(m)
	if err != nil {
		return raw
	}
	return out
}

// resolveRepoRoot walks up from cwd to the nearest .git directory; falls back to
// cwd when not in a git repo (every directory can host a daemon).
func resolveRepoRoot() (string, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	dir := cwd
	for {
		if fi, err := os.Stat(filepath.Join(dir, ".git")); err == nil && fi.IsDir() {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return cwd, nil
		}
		dir = parent
	}
}

func fmtUptime(d time.Duration) string {
	d = d.Round(time.Second)
	switch {
	case d < time.Minute:
		return strconv.Itoa(int(d.Seconds())) + "s"
	case d < time.Hour:
		return strconv.Itoa(int(d.Minutes())) + "m"
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh%dm", int(d.Hours()), int(d.Minutes())%60)
	default:
		return fmt.Sprintf("%dd%dh", int(d.Hours())/24, int(d.Hours())%24)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "claude+:", strings.TrimSpace(err.Error()))
	os.Exit(1)
}
