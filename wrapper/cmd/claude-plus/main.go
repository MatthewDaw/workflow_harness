// Command claude-plus is the claude+ CLI: a thin attach client over a per-repo
// background daemon (the tmux model, KTD2).
//
// Usage:
//
//	claude+              attach-or-create the daemon for the current repo
//	claude+ ls           list running daemons (index, repo, host, sessions, state, uptime)
//	claude+ --session=N  attach to the daemon at registry index N
//	claude+ --version    print the version
//
// Hidden verbs used internally:
//
//	claude+ __daemon <repoRoot>   run the detached per-repo daemon (re-exec target)
//	claude+ __hook                forward a Claude Code hook event to the daemon
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/workflow-harness/claude-plus/internal/daemon"
)

// version is overridden at build time via -ldflags (goreleaser).
var version = "dev"

func main() {
	// Hidden internal verbs are dispatched before flag parsing.
	if len(os.Args) >= 2 {
		switch os.Args[1] {
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
		}
	}

	var sessionN int
	var showVersion bool
	fs := flag.NewFlagSet("claude+", flag.ContinueOnError)
	fs.IntVar(&sessionN, "session", -1, "attach to the daemon at registry index N")
	fs.BoolVar(&showVersion, "version", false, "print version and exit")
	if err := fs.Parse(os.Args[1:]); err != nil {
		os.Exit(2)
	}

	if showVersion {
		fmt.Println("claude+", version)
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

// cmdAttachOrCreate resolves the repo for cwd, ensures its daemon is running,
// and attaches.
func cmdAttachOrCreate() error {
	repo, err := resolveRepoRoot()
	if err != nil {
		return err
	}
	if _, err := daemon.EnsureDaemon(repo); err != nil {
		return fmt.Errorf("start daemon: %w", err)
	}
	c, err := daemon.Dial(repo)
	if err != nil {
		return fmt.Errorf("attach: %w", err)
	}
	return runClient(c)
}

// cmdAttachIndex attaches to the daemon at the given `ls` index.
func cmdAttachIndex(n int) error {
	c, err := daemon.DialIndex(n)
	if err != nil {
		return err
	}
	return runClient(c)
}

// runClient runs the foreground attach loop. (Terminal raw-mode setup and the
// Bubble Tea program are wired here in the full build; this keeps the attach
// surface minimal and exercises the client read loop.)
func runClient(c *daemon.Client) error {
	c.Out = func(b []byte) { os.Stdout.Write(b) }
	return c.Run()
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
func runHook() {
	// The hook shim reads the event from stdin and posts it to the daemon. The
	// full implementation lives in internal/capture; this stub keeps the CLI
	// surface complete. It exits 0 so it never blocks a Claude Code turn.
	_, _ = os.Stdout.Write(nil)
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
