package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/workflow-harness/claude-plus/internal/config"
	"github.com/workflow-harness/claude-plus/internal/workflow"
)

// cmdRunWorkflow is the `claude+ run-workflow <name> [--project <id>]` verb: the
// standalone, headless executor for a workflow DAG. It resolves the repo + creds
// like cmdSync, fetches the workflow definition + the project's enabled set over
// the device-token HTTP client (falling back to the materialized
// ~/.claude+/.../workflows/<name>.json), creates a run, topo-sorts the DAG, and
// executes it wave by wave with bounded concurrency — running each node's agent
// headlessly (`claude -p`, the judge/title pattern) plus its rerun-until-done
// loop, and POSTing a node-status update after each transition. It prints progress
// to stdout and finalizes by reporting the overall run status.
//
// It never touches the PTY mux — node runs are tagged CLAUDE_PLUS_WORKFLOW=1 so
// the installed hook shim skips them (no phantom sessions). Every loop is bounded
// by the per-node MaxRuns cap and a per-node context timeout, so the executor can
// never run forever or fan out unboundedly.
func cmdRunWorkflow(args []string) error {
	name, projectFlag, err := parseWorkflowArgs(args)
	if err != nil {
		return err
	}

	// Resolve the repo root the agents run in, plus the HQ API base + device token
	// and the project id — exactly the inputs cmdSync resolves, so the executor
	// reads the same project record the sync pipeline materializes.
	repoRoot, err := resolveRepoRoot()
	if err != nil {
		return err
	}
	base, token, ok := loadWorkflowCreds()
	if !ok {
		return fmt.Errorf("not signed in to HQ (run `claude+ login`)")
	}
	projectID := projectFlag
	if projectID == "" {
		projectID = config.ProjectIDFor(repoRoot)
	}

	cl := newWorkflowClient(base, token)

	// Fetch the workflow definition: prefer HQ's catalog (GET /workflows, find by
	// name) so the executor runs the authoritative current spec; fall back to the
	// materialized on-disk JSON when HQ is unreachable or the name is absent there.
	wf, err := fetchWorkflow(cl, repoRoot, name)
	if err != nil {
		return err
	}
	if len(wf.Nodes) == 0 {
		return fmt.Errorf("workflow %q has no nodes", name)
	}

	// Topo-sort up front so a cyclic / unschedulable DAG fails before any run record
	// or subprocess is created.
	waves, err := workflow.TopoSort(wf.Nodes)
	if err != nil {
		return fmt.Errorf("workflow %q: %w", name, err)
	}

	// Create the run record so the web overlay has a runId + a per-node map to poll.
	runID, err := cl.createRun(name, projectID)
	if err != nil {
		return fmt.Errorf("create run for %q: %w", name, err)
	}
	fmt.Printf("workflow %q: run %s (%d node(s), %d wave(s))\n", name, runID, len(wf.Nodes), len(waves))

	final := executeWaves(cl, repoRoot, name, projectID, runID, wf, waves)
	fmt.Printf("workflow %q: run %s finished — %s\n", name, runID, final)
	if final == "failed" {
		return fmt.Errorf("workflow %q run %s failed", name, runID)
	}
	return nil
}

// parseWorkflowArgs extracts the required workflow name and the optional
// `--project <id>` (also accepting `--project=<id>`). The name is the first
// non-flag positional.
func parseWorkflowArgs(args []string) (name, project string, err error) {
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "--project":
			if i+1 >= len(args) {
				return "", "", fmt.Errorf("--project requires a value")
			}
			project = args[i+1]
			i++
		case strings.HasPrefix(a, "--project="):
			project = strings.TrimPrefix(a, "--project=")
		case strings.HasPrefix(a, "-"):
			return "", "", fmt.Errorf("unknown flag %q", a)
		default:
			if name == "" {
				name = a
			}
		}
	}
	if name == "" {
		return "", "", fmt.Errorf("usage: claude+ run-workflow <name> [--project <id>]")
	}
	return name, project, nil
}

// loadWorkflowCreds resolves the HQ API base + device token the executor uses
// for the run REST calls: the env overrides (CLAUDE_PLUS_API_URL /
// CLAUDE_PLUS_TOKEN) win, else the credentials file written by `claude+ login`
// (read via config.LoadCredentials).
func loadWorkflowCreds() (base, token string, ok bool) {
	base, _ = config.APIBase()
	token = os.Getenv("CLAUDE_PLUS_TOKEN")
	if base == "" || token == "" {
		creds, credsOK := config.LoadCredentials()
		if !credsOK {
			return "", "", false
		}
		if token == "" {
			token = creds.Token
		}
		if base == "" {
			base = creds.APIBase
		}
	}
	if base == "" || token == "" {
		return "", "", false
	}
	return base, token, true
}

// fetchWorkflow returns the workflow definition to execute. It first asks HQ
// (GET /workflows) and picks the record whose name matches, so the executor runs
// the authoritative catalog spec. On any HTTP error, or when the name is not in
// the catalog, it falls back to the materialized on-disk spec at
// ~/.claude+/roots/<slug>/workflows/<name>.json (what `claude+ sync` wrote).
func fetchWorkflow(cl *workflowClient, repoRoot, name string) (workflow.Workflow, error) {
	if wfs, err := cl.listWorkflows(); err == nil {
		for _, wf := range wfs {
			if wf.Name == name {
				return wf, nil
			}
		}
	}
	root, err := config.ProjectConfigDir(repoRoot)
	if err != nil {
		return workflow.Workflow{}, err
	}
	path := filepath.Join(root, "workflows", name+".json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return workflow.Workflow{}, fmt.Errorf("workflow %q not found in HQ catalog or on disk (%s): %w", name, path, err)
	}
	var wf workflow.Workflow
	if err := json.Unmarshal(raw, &wf); err != nil {
		return workflow.Workflow{}, fmt.Errorf("decode local workflow %q: %w", name, err)
	}
	return wf, nil
}

// executeWaves runs the topo-sorted DAG wave by wave, returning the overall run
// status ("done" if every node settled successfully, else "failed"). Within a
// wave the nodes run concurrently under a bounded token-bucket limiter (like the
// daemon's judge limiter), since their dependencies are by construction already
// done. Each node's captured output is recorded so the next wave's dependents see
// it as context. A node that fails (run error or rerun cap hit unsatisfied) marks
// the run failed but the remaining ready nodes still finish, so the run reports a
// complete picture rather than aborting mid-wave.
func executeWaves(cl *workflowClient, repoRoot, name, projectID, runID string, wf workflow.Workflow, waves [][]string) string {
	byID := make(map[string]workflow.Node, len(wf.Nodes))
	for _, n := range wf.Nodes {
		byID[n.ID] = n
	}

	// outputs accumulates each completed node's final output (guarded — a wave runs
	// nodes concurrently). failed flips to true on the first node failure.
	var mu sync.Mutex
	outputs := map[string]string{}
	failed := false

	// Bounded concurrency: a buffered channel of permits shared across the wave,
	// so a wide wave never fans out into an unbounded number of headless model
	// calls. Acquiring BLOCKS (every ready node must eventually run), unlike the
	// daemon's judge limiter's non-blocking skip.
	sem := make(chan struct{}, workflowConcurrency)

	for _, wave := range waves {
		var wg sync.WaitGroup
		for _, id := range wave {
			node := byID[id]
			wg.Add(1)
			go func(node workflow.Node) {
				defer wg.Done()
				sem <- struct{}{}
				defer func() { <-sem }()

				// Snapshot the dependency outputs this node needs (completed in prior
				// waves) under the lock.
				mu.Lock()
				deps := make(map[string]string, len(node.DependsOn))
				for _, dep := range node.DependsOn {
					deps[dep] = outputs[dep]
				}
				mu.Unlock()

				out, ok := runOneNode(cl, repoRoot, name, projectID, runID, node, byID, deps)
				mu.Lock()
				outputs[node.ID] = out
				if !ok {
					failed = true
				}
				mu.Unlock()
			}(node)
		}
		wg.Wait()
	}

	if failed {
		return "failed"
	}
	return "done"
}

// runOneNode runs a single node end-to-end: flip it to `running`, run its agent
// once, then (if it carries a rerun rule) loop until done or the MaxRuns cap, and
// finally flip it to `done`/`failed`. It POSTs a node-status update after each
// transition so the web overlay tracks live progress, and returns the node's final
// output and whether it succeeded.
func runOneNode(cl *workflowClient, repoRoot, name, projectID, runID string, node workflow.Node, byID map[string]workflow.Node, deps map[string]string) (string, bool) {
	fmt.Printf("  node %q (%s): running\n", node.ID, node.Agent)
	cl.postNode(name, projectID, runID, node.ID, nodeUpdate{State: "running"})

	ctx := context.Background()
	out, runs, ok := workflow.RunWithRerun(ctx, repoRoot, node, byID, deps)
	tail := outputTail(out)
	if !ok {
		fmt.Printf("  node %q: failed after %d run(s)\n", node.ID, runs)
		cl.postNode(name, projectID, runID, node.ID, nodeUpdate{State: "failed", Runs: runs, OutputTail: tail})
		return out, false
	}
	fmt.Printf("  node %q: done after %d run(s)\n", node.ID, runs)
	cl.postNode(name, projectID, runID, node.ID, nodeUpdate{State: "done", Runs: runs, OutputTail: tail})
	return out, true
}

// outputTail clips a node's captured stdout to the last slice stored on the run
// record (the tooltip in the web overlay). A short output passes through whole.
func outputTail(s string) string {
	const max = 2000
	s = strings.TrimSpace(s)
	if len(s) <= max {
		return s
	}
	return s[len(s)-max:]
}

// workflowConcurrency caps the number of nodes running simultaneously within a
// wave — a small fixed bucket, like the daemon's judge limiter (n=2), so a wide
// wave never spawns an unbounded burst of headless agents.
const workflowConcurrency = 2

// -----------------------------------------------------------------------------
// workflowClient — the device-token HTTP client for the run REST surface: thin
// wrappers binding this command's base URL + token onto config.DoJSON.
// -----------------------------------------------------------------------------

type workflowClient struct {
	base   string
	token  string
	client *http.Client
}

func newWorkflowClient(base, token string) *workflowClient {
	return &workflowClient{
		base:   strings.TrimRight(base, "/"),
		token:  token,
		client: &http.Client{Timeout: 15 * time.Second},
	}
}

// listWorkflows reads the org catalog (GET /workflows) and decodes it into the
// executor's workflow structs.
func (c *workflowClient) listWorkflows() ([]workflow.Workflow, error) {
	var resp struct {
		Workflows []workflow.Workflow `json:"workflows"`
	}
	if err := c.getJSON("/workflows", &resp); err != nil {
		return nil, err
	}
	return resp.Workflows, nil
}

// createRun POSTs to /workflows/{name}/runs and returns the server-minted runId.
func (c *workflowClient) createRun(name, projectID string) (string, error) {
	var resp struct {
		Run struct {
			RunID string `json:"runId"`
		} `json:"run"`
	}
	path := "/workflows/" + url.PathEscape(name) + "/runs"
	if err := c.postJSON(path, map[string]any{"projectId": projectID}, &resp); err != nil {
		return "", err
	}
	if resp.Run.RunID == "" {
		return "", fmt.Errorf("create run: empty runId in response")
	}
	return resp.Run.RunID, nil
}

// nodeUpdate is the partial node-state body POSTed on each transition; the backend
// merges only the fields present. Runs/OutputTail are omitted on a bare state
// flip (e.g. `running`) so they don't clobber the node's accumulating count.
type nodeUpdate struct {
	ProjectID  string `json:"projectId"`
	State      string `json:"state"`
	Runs       int    `json:"runs,omitempty"`
	OutputTail string `json:"outputTail,omitempty"`
}

// postNode reports one node transition to /workflows/{name}/runs/{runId}/nodes/
// {nodeId}. Best-effort: a status-report failure is logged to stderr but never
// aborts the run (the agents have already done the work; losing a status ping must
// not fail the execution).
func (c *workflowClient) postNode(name, projectID, runID, nodeID string, u nodeUpdate) {
	path := "/workflows/" + url.PathEscape(name) + "/runs/" + url.PathEscape(runID) + "/nodes/" + url.PathEscape(nodeID)
	u.ProjectID = projectID
	if err := c.postJSON(path, u, nil); err != nil {
		fmt.Fprintf(os.Stderr, "claude+: workflow node status update failed (%s): %v\n", nodeID, err)
	}
}

func (c *workflowClient) getJSON(path string, dst any) error {
	return config.DoJSON(c.client, http.MethodGet, c.base+path, c.token, nil, dst, path)
}

// postJSON POSTs a JSON payload and decodes the response into dst (nil to ignore
// the body). It accepts any 2xx (POST /runs returns 201 Created).
func (c *workflowClient) postJSON(path string, payload, dst any) error {
	return config.DoJSON(c.client, http.MethodPost, c.base+path, c.token, payload, dst, path)
}
