package config

import (
	"fmt"
	"os"
)

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

// DriftCount is the single number the status-bar drift meter shows: items that
// diverge from HQ (push + pull + differs). Local read errors are excluded — they
// are a local-file problem surfaced separately, not HQ drift.
func (r DriftReport) DriftCount() int {
	return r.NeedsPush + r.NeedsPull + r.Differs
}

// RemoteSource abstracts HQ's scoped registry: it yields the effective set (with
// content hashes) and actuates a reconcile. Keeping it an interface lets the
// pure drift + reconcile logic be tested with an in-memory fake while the real
// HTTP implementation (remote.go) talks to Command HQ. HQ stays read-only for
// GitHub progress, but agents/skills are HQ-owned, so push/pull here is the one
// place the wrapper writes back to HQ (user scope only).
type RemoteSource interface {
	// Fetch returns HQ's effective agents+skills for this user+project, each with
	// a content hash comparable to the local hash. Malformed remote entries are
	// skipped by the implementation, not returned as errors.
	Fetch() ([]RemoteItem, error)
	// Body returns the full content of a remote item, used to materialize a pull.
	Body(item RemoteItem) (string, error)
	// Push uploads a local-only item to HQ at the caller's user scope.
	Push(item Item, body string) error
}

// ComputeDrift reads the given per-project registry (`plus`), fetches HQ's
// effective set, and diffs them. It is the passive meter feed (no writes) — the
// daemon calls it on a poll and forwards DriftCount() to the status bar.
func ComputeDrift(src RemoteSource, plus string) (DriftReport, error) {
	local, err := ReadLocal(plus)
	if err != nil {
		return DriftReport{}, err
	}
	remote, err := src.Fetch()
	if err != nil {
		return DriftReport{}, err
	}
	return Diff(local, remote), nil
}

// Reconcile actuates a drift report against HQ: HQ-only items (needs_pull) are
// written into the given per-project tree (`plus`); local-only items (needs_push)
// are pushed to HQ user scope. `differs` rows are left for the user to resolve
// explicitly (we never silently overwrite an edited definition). Per-item
// failures are collected and do not abort the run, so the operation is safe to
// retry; running it again on a converged set is a no-op (idempotent). Returns
// counts actuated plus any non-fatal errors.
func Reconcile(report DriftReport, src RemoteSource, local []Item, plus string) (pulled, pushed int, errs []error) {
	byKey := map[string]Item{}
	for _, it := range local {
		byKey[string(it.Kind)+"/"+it.Name] = it
	}
	for _, row := range report.Rows {
		switch row.Drift {
		case DriftNeedsPull:
			ri := RemoteItem{Kind: row.Kind, Name: row.Name, Scope: row.Scope}
			body, err := src.Body(ri)
			if err != nil {
				errs = append(errs, fmt.Errorf("pull %s/%s: %w", row.Kind, row.Name, err))
				continue
			}
			if err := ApplyPulled(plus, ri, body); err != nil {
				errs = append(errs, fmt.Errorf("apply %s/%s: %w", row.Kind, row.Name, err))
				continue
			}
			pulled++
		case DriftNeedsPush:
			it, ok := byKey[string(row.Kind)+"/"+row.Name]
			if !ok {
				continue
			}
			b, err := os.ReadFile(it.Path)
			if err != nil {
				errs = append(errs, fmt.Errorf("read %s/%s: %w", row.Kind, row.Name, err))
				continue
			}
			if err := src.Push(it, string(b)); err != nil {
				errs = append(errs, fmt.Errorf("push %s/%s: %w", row.Kind, row.Name, err))
				continue
			}
			pushed++
		}
	}
	return pulled, pushed, errs
}
