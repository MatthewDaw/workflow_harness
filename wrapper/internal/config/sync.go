package config

// RemoteItem is HQ's view of an effective (resolved) agent or skill for the
// current user+project. Hash is the content hash HQ stores so drift can be
// detected without fetching full bodies.
type RemoteItem struct {
	Kind  Kind   `json:"kind"`
	Name  string `json:"name"`
	Scope string `json:"scope"` // org | user#uid | proj#pid
	Hash  string `json:"hash"`
}

// DriftKind classifies how a local item compares to HQ.
type DriftKind string

const (
	DriftNeedsPush DriftKind = "needs_push" // local-only: push to HQ user scope
	DriftNeedsPull DriftKind = "needs_pull" // HQ-only: pull down to local
	DriftDiffers   DriftKind = "differs"    // present both sides, content hash mismatch
	DriftInSync    DriftKind = "in_sync"    // identical
	DriftError     DriftKind = "error"      // malformed local file
)

// DriftRow is one reconciled comparison surfaced in the Agents/Skills tab.
type DriftRow struct {
	Kind  Kind      `json:"kind"`
	Name  string    `json:"name"`
	Drift DriftKind `json:"drift"`
	Scope string    `json:"scope,omitempty"` // HQ scope when known
	Err   string    `json:"err,omitempty"`
}

// DriftReport is the full comparison plus convenience counts for the status line
// (e.g. "⚠ 2 skills differ").
type DriftReport struct {
	Rows         []DriftRow `json:"rows"`
	NeedsPush    int        `json:"needsPush"`
	NeedsPull    int        `json:"needsPull"`
	Differs      int        `json:"differs"`
	Errors       int        `json:"errors"`
}

// Diff compares local items against HQ's effective set and produces a drift
// report. Pure and deterministic so it is trivially testable (no I/O).
func Diff(local []Item, remote []RemoteItem) DriftReport {
	type key struct {
		kind Kind
		name string
	}
	remoteByKey := map[key]RemoteItem{}
	for _, r := range remote {
		remoteByKey[key{r.Kind, r.Name}] = r
	}
	seen := map[key]bool{}

	var report DriftReport
	for _, l := range local {
		k := key{l.Kind, l.Name}
		seen[k] = true
		if l.Err != "" {
			report.Rows = append(report.Rows, DriftRow{Kind: l.Kind, Name: l.Name, Drift: DriftError, Err: l.Err})
			report.Errors++
			continue
		}
		r, ok := remoteByKey[k]
		switch {
		case !ok:
			report.Rows = append(report.Rows, DriftRow{Kind: l.Kind, Name: l.Name, Drift: DriftNeedsPush})
			report.NeedsPush++
		case r.Hash != l.Hash:
			report.Rows = append(report.Rows, DriftRow{Kind: l.Kind, Name: l.Name, Drift: DriftDiffers, Scope: r.Scope})
			report.Differs++
		default:
			report.Rows = append(report.Rows, DriftRow{Kind: l.Kind, Name: l.Name, Drift: DriftInSync, Scope: r.Scope})
		}
	}
	// HQ-only items need a pull.
	for _, r := range remote {
		if seen[key{r.Kind, r.Name}] {
			continue
		}
		report.Rows = append(report.Rows, DriftRow{Kind: r.Kind, Name: r.Name, Drift: DriftNeedsPull, Scope: r.Scope})
		report.NeedsPull++
	}
	return report
}

// InSync reports whether a report shows no divergence (used to gate the warning
// indicator).
func (r DriftReport) InSync() bool {
	return r.NeedsPush == 0 && r.NeedsPull == 0 && r.Differs == 0 && r.Errors == 0
}
