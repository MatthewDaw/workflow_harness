// Package workflow is the Go execution engine for Command HQ workflows: it runs a
// DAG of catalog agents headlessly and reports live status back to HQ.
//
// A workflow is the structured spec the wrapper materializes at
// ~/.claude+/workflows/<name>.json (see internal/config). This package decodes
// that spec into Go structs (mirroring the workflowSchema DTO), topologically
// sorts the DAG into waves (topo.go), and runs each node's agent via a one-shot,
// time-boxed headless `claude -p` — the same spawn pattern internal/judge and
// internal/title use (run.go). Each node may carry a rerun-until-done rule that
// loops the node until its own end-criteria is met (a Haiku judge) or until a
// checker node declares it done (the generator↔checker loop), both bounded by a
// MaxRuns safety cap.
//
// The cmd/claude-plus run-workflow verb (workflow.go) drives this package: it
// resolves repoRoot + creds, fetches the workflow def + creates a run over the
// device-token HTTP client, then schedules the waves with bounded concurrency and
// POSTs node-status transitions as the run progresses.
package workflow

// Workflow is the structured spec the wrapper materializes at
// workflows/<name>.json. It mirrors the workflowSchema DTO envelope (name/scope/
// kind/description) plus the DAG `Nodes`. There is no bundle concept (v1), so
// Kind is the single literal "workflow".
type Workflow struct {
	Name        string `json:"name"`
	Kind        string `json:"kind"`
	Description string `json:"description"`
	Nodes       []Node `json:"nodes"`
}

// Node is one node of the DAG: a pointer to a catalog `Agent` plus its per-node
// task `Prompt`, upstream dependency edges (`DependsOn` node ids), and an optional
// rerun-until-done rule. Mirrors workflowNodeSchema.
type Node struct {
	ID        string   `json:"id"`
	Agent     string   `json:"agent"`
	Label     string   `json:"label"`
	Prompt    string   `json:"prompt"`
	DependsOn []string `json:"dependsOn"`
	Rerun     *Rerun   `json:"rerun,omitempty"`
}

// Rerun is a node's rerun-until-done rule. `self` loops until a Haiku judge
// declares <EndCriteria> met; `declared-by` loops until the checker node named by
// DeclaredBy declares the node's output done. MaxRuns is the mandatory safety cap
// so a loop can never run forever. Mirrors workflowRerunSchema.
type Rerun struct {
	Mode        string `json:"mode"`
	EndCriteria string `json:"endCriteria"`
	DeclaredBy  string `json:"declaredBy,omitempty"`
	MaxRuns     int    `json:"maxRuns"`
}
