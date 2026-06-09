package workflow

import (
	"fmt"
	"sort"
)

// TopoSort orders the DAG's nodes into WAVES via Kahn's algorithm over the
// DependsOn edges: wave 0 is every node with no dependencies, wave 1 is every
// node whose deps are all satisfied by wave 0, and so on. The executor runs a
// whole wave concurrently (bounded), then advances — so the wave structure is
// exactly the scheduler's parallelism map.
//
// Only DependsOn forms the DAG edges; a `declared-by` rerun is a CONTROL edge
// (excluded here, matching the DTO superRefine) so a checker referencing a node
// that depends on it never appears as a cycle — the executor bounds that loop
// with MaxRuns instead.
//
// A dangling DependsOn (an id with no matching node) is skipped rather than
// failing: the DTO superRefine already rejects those at author time, so a
// materialized spec carries only resolvable edges; skipping keeps a hand-edited
// file robust. A genuine cycle (some node never reaches indegree 0) returns an
// error — the workflow cannot be executed.
func TopoSort(nodes []Node) ([][]string, error) {
	// indegree[id] = number of (resolvable) deps still unsatisfied; adj[dep] = the
	// nodes that depend on dep (edge dep -> node).
	indegree := make(map[string]int, len(nodes))
	adj := make(map[string][]string, len(nodes))
	ids := make(map[string]bool, len(nodes))
	for _, n := range nodes {
		indegree[n.ID] = 0
		ids[n.ID] = true
	}
	for _, n := range nodes {
		for _, dep := range n.DependsOn {
			if !ids[dep] {
				continue // dangling edge (rejected by the DTO at author time)
			}
			adj[dep] = append(adj[dep], n.ID)
			indegree[n.ID]++
		}
	}

	// Seed the first wave with every zero-indegree node, sorted for determinism so
	// the wave order is stable across runs (and across map iteration order).
	var waves [][]string
	cur := zeroIndegree(indegree)
	processed := 0
	for len(cur) > 0 {
		sort.Strings(cur)
		waves = append(waves, cur)
		processed += len(cur)
		var next []string
		for _, id := range cur {
			for _, m := range adj[id] {
				indegree[m]--
				if indegree[m] == 0 {
					next = append(next, m)
				}
			}
		}
		cur = next
	}

	if processed < len(ids) {
		return nil, fmt.Errorf("workflow dependsOn graph has a cycle")
	}
	return waves, nil
}

// zeroIndegree collects every node currently at indegree 0. Used to seed the
// first wave; subsequent waves are built incrementally as edges are peeled off.
func zeroIndegree(indegree map[string]int) []string {
	var out []string
	for id, deg := range indegree {
		if deg == 0 {
			out = append(out, id)
		}
	}
	return out
}
