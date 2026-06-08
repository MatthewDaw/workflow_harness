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
	// AgentSkills returns the skill names the named agent depends on
	// (agentSchema.skills), captured during the most recent Fetch. Reconcile uses
	// it to ensure an agent's skills are materialized after the agent itself
	// (U-Agent-Deps). An unknown agent yields nil.
	AgentSkills(agentName string) []string
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
	// Track which skills are present (locally already, or pulled during this run) so
	// the agent-dependency pass (U-Agent-Deps) only pulls skills that are still
	// missing — and so it never double-pulls a skill the main loop already handled.
	skillPresent := map[string]bool{}
	for _, it := range local {
		if it.Kind == KindSkill {
			skillPresent[it.Name] = true
		}
	}
	// Agents we materialized (or that are already present) and must ensure deps for.
	var ensureDepsFor []string
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
			if row.Kind == KindSkill {
				skillPresent[row.Name] = true
			}
			if row.Kind == KindAgent {
				ensureDepsFor = append(ensureDepsFor, row.Name)
			}
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
		case DriftInSync, DriftDiffers:
			// An agent that is already present (or locally edited) still needs its
			// skill deps ensured — the agent file landing does not guarantee its
			// skills are on disk (U-Agent-Deps).
			if row.Kind == KindAgent {
				ensureDepsFor = append(ensureDepsFor, row.Name)
			}
		}
	}

	// U-Agent-Deps: ensure every skill each materialized/present agent depends on is
	// on disk, pulling any that are still missing. The dep skill's body is available
	// from the same Fetch (the backend union-adds an enabled agent's skills into the
	// project's enabled set, so they are cached). A dep with no available body (or a
	// write failure) is a non-fatal error — the agent file still landed, and the gate
	// will flag the missing skill — so it never aborts the run.
	depPulled := ensureAgentSkillDeps(src, plus, ensureDepsFor, skillPresent, &errs)
	pulled += depPulled
	return pulled, pushed, errs
}

// ensureAgentSkillDeps pulls each missing skill dependency of the given agents into
// `plus`. skillPresent tracks skills already on disk / pulled this run (and is
// updated as deps are pulled, so two agents sharing a dep pull it once). It returns
// the number of dep skills newly pulled and appends any non-fatal errors. A dep
// already present is a no-op (idempotent). Pure over the source's cached state so a
// re-run on a converged set actuates nothing.
func ensureAgentSkillDeps(src RemoteSource, plus string, agents []string, skillPresent map[string]bool, errs *[]error) (pulled int) {
	for _, agent := range agents {
		for _, skill := range src.AgentSkills(agent) {
			if skill == "" || skillPresent[skill] {
				continue
			}
			ri := RemoteItem{Kind: KindSkill, Name: skill}
			body, err := src.Body(ri)
			if err != nil {
				*errs = append(*errs, fmt.Errorf("agent %q dep skill %q: %w", agent, skill, err))
				// Mark present-attempted so a sibling agent does not retry the same
				// unavailable dep and pile up duplicate errors.
				skillPresent[skill] = true
				continue
			}
			if err := ApplyPulled(plus, ri, body); err != nil {
				*errs = append(*errs, fmt.Errorf("agent %q dep skill %q apply: %w", agent, skill, err))
				skillPresent[skill] = true
				continue
			}
			skillPresent[skill] = true
			pulled++
		}
	}
	return pulled
}
