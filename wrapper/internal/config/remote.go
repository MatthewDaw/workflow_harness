package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

// orgScopeFor returns the org-scoped ScopeRef the wrapper pushes with. The
// catalog is org-only now (the 3-tier scope is retired for skills+agents), so
// local-only pushes go up at org scope, matching the scope HQ serves back.
func orgScopeFor(org string) map[string]string {
	return map[string]string{"tier": "org", "id": org}
}

// HTTPRemoteSource is the production RemoteSource: it reads HQ's effective
// agents/skills over the REST API and computes content hashes comparable to the
// local ones.
//
// Hash parity note: HQ stores an agent's body as `prompt` and a skill's as
// `description`. We hash exactly that text (via the same hashContent used
// locally), and ApplyPulled writes exactly that text, so a pulled item reads
// back in-sync. A locally authored file that carries extra frontmatter will
// hash differently and surface as `differs` — which is correct: it does differ
// from HQ's stored form until reconciled.
type HTTPRemoteSource struct {
	BaseURL   string // HQ REST base, e.g. https://api.hq.example.com
	Token     string // bearer/device token
	ProjectID string // narrows the effective set to this project
	Client    *http.Client

	// Everything below is captured by Fetch and read by Body / Push /
	// AgentSkills / DeclaredSkills / DeclaredAgents, which can run on different
	// goroutines (the drift poll loop and a per-session reconcile share one
	// source). bodiesMu guards all of it; Fetch publishes the whole snapshot
	// under one lock so a reader never observes a half-populated state.
	bodiesMu sync.RWMutex
	// bodies caches each item's content so Body() (a subsequent reconcile pull)
	// does not re-hit the network.
	bodies map[string]string
	// orgID is the catalog's org, learned from the scope HQ stamps on returned
	// items. Push uses it so a local-only item lands at the same org scope HQ
	// serves back (the catalog is org-only).
	orgID string
	// agentSkills maps an enabled agent's name to the skill names it depends on
	// (agentSchema.skills), so Reconcile can materialize each dep skill after
	// the agent.
	agentSkills map[string][]string
	// declaredSkills / declaredAgents are the project's FULL declared enabled
	// sets (enabled names plus live-flattened bundle members), kept for the
	// verify gate; see RemoteSource.DeclaredSkills/DeclaredAgents for the
	// contract.
	declaredSkills []string
	declaredAgents []string
}

// NewHTTPRemoteSource builds a source with a bounded HTTP client.
func NewHTTPRemoteSource(baseURL, token, projectID string) *HTTPRemoteSource {
	return &HTTPRemoteSource{
		BaseURL:   strings.TrimRight(baseURL, "/"),
		Token:     token,
		ProjectID: projectID,
		Client:    &http.Client{Timeout: 10 * time.Second},
	}
}

type remoteAgent struct {
	Name  string `json:"name"`
	Scope scope  `json:"scope"`
	// Kind is "agent" or "bundle". A bundle is not materialized itself; its
	// `resolvedMembers` (transitively flattened leaf agent names, annotated by the
	// backend on GET /agents) are expanded into the effective agent set when the
	// project has opted into the bundle. `members` is the un-flattened fallback.
	// Mirrors remoteSkill's Kind/ResolvedMembers/Members.
	Kind            string   `json:"kind"`
	ResolvedMembers []string `json:"resolvedMembers"`
	Members         []string `json:"members"`
	// Description, Tools, and Model are the structured fields HQ stores for an
	// agent. They were previously decoded-but-discarded (only Prompt was
	// materialized); renderAgentFile now folds them into YAML frontmatter so a
	// pulled agent is a valid Claude Code subagent file, not a bare prompt.
	Description string   `json:"description"`
	Tools       []string `json:"tools"`
	Model       string   `json:"model"`
	Prompt      string   `json:"prompt"`
	// Skills are the names of the skills this agent depends on (agentSchema.skills).
	// After materializing the agent, the wrapper must ensure each of these skills is
	// present in <root>/skills, pulling any that are missing — an
	// agent whose skills are absent is a broken install even if the agent file lands.
	Skills []string `json:"skills"`
}

// renderAgentFile materializes a remoteAgent into the on-disk Claude Code
// subagent format: a YAML frontmatter block (name/description, plus tools and
// model only when set) followed by the prompt body and exactly one trailing
// newline. This is the SAME string the wrapper hashes in Fetch, so a freshly
// pulled agent reads back in-sync (hash parity), mirroring how a skill's body
// is the full SKILL.md text.
//
// Omission rules per the shared contract: the `tools:` line is dropped when
// Tools is empty; the `model:` line is dropped when Model is "" (note that the
// literal "inherit" is a real value and IS emitted).
func renderAgentFile(a remoteAgent) string {
	var b strings.Builder
	b.WriteString("---\n")
	b.WriteString("name: " + a.Name + "\n")
	b.WriteString("description: " + a.Description + "\n")
	if len(a.Tools) > 0 {
		b.WriteString("tools: " + strings.Join(a.Tools, ", ") + "\n")
	}
	if a.Model != "" {
		b.WriteString("model: " + a.Model + "\n")
	}
	b.WriteString("---\n")
	// Exactly one trailing newline after the prompt: strip any the prompt already
	// carries, then add one. An empty prompt yields just the frontmatter + newline.
	b.WriteString(strings.TrimRight(a.Prompt, "\n"))
	b.WriteString("\n")
	return b.String()
}

// parseAgentFile splits a materialized agent file back into its structured
// frontmatter fields and the body WITHOUT frontmatter. It is the inverse of
// renderAgentFile and exists for the Push round-trip: a local agent file
// carries frontmatter, so Push must NOT send the whole thing as `prompt` (that
// would double-wrap on the next materialize). It reads exactly the keys
// renderAgentFile writes via the shared frontmatter.go helpers.
//
// If the file has no leading `---` frontmatter block, or the block is
// unterminated (e.g. a legacy bare-prompt agent), the whole text is returned
// verbatim as the body with empty structured fields — that prompt still
// round-trips losslessly.
func parseAgentFile(content string) (name, description, model string, tools []string, body string) {
	fm, fmBody, ok := frontmatterBlock(content)
	if !ok {
		return "", "", "", nil, content
	}
	body = fmBody
	frontmatterFields(fm, func(key, val string, _ bool) {
		switch key {
		case "name":
			name = val
		case "description":
			description = val
		case "model":
			model = val
		case "tools":
			for _, t := range strings.Split(val, ",") {
				if t = strings.TrimSpace(t); t != "" {
					tools = append(tools, t)
				}
			}
		}
	})
	return name, description, model, tools, body
}

type remoteSkill struct {
	Name        string `json:"name"`
	Scope       scope  `json:"scope"`
	Description string `json:"description"`
	// Kind is "skill" or "bundle". A bundle is not materialized itself; its
	// `resolvedMembers` (transitively flattened leaf skill names, annotated by the
	// backend on GET /skills) are expanded into the effective set when the project
	// has opted into the bundle. `members` is the un-flattened fallback.
	Kind            string   `json:"kind"`
	ResolvedMembers []string `json:"resolvedMembers"`
	Members         []string `json:"members"`
	// Body is the full SKILL.md text HQ serves on GET /skills and
	// GET /skills/{name}. When present it is the authoritative SKILL.md content to
	// materialize locally; older HQ responses omit it, so we fall back to
	// Description for hashing and pulling to stay backward compatible.
	Body string `json:"body"`
	// Files is the WHOLE skill directory tree: relative path within
	// the skill dir -> file contents (e.g. "SKILL.md", "scripts/run.sh"). When HQ
	// serves it, the wrapper materializes every file (not just SKILL.md) and hashes
	// the whole tree so a sibling edit drifts. Legacy records omit it and fall back
	// to the Body/Description single-file path (fully back-compatible).
	Files map[string]string `json:"files"`
}

// skillFilesFor resolves a remoteSkill to the files map the wrapper materializes
// and hashes. Precedence: an explicit `files` map (whole-directory skill) wins;
// otherwise the single SKILL.md body (Body, then Description) is wrapped as
// {"SKILL.md": content}. A `files` map that omits SKILL.md is backfilled from the
// Body/Description so the entry file is never missing. The result feeds BOTH the
// hash (hashSkillFiles) and the materialized body (encodeSkillBody), keeping a
// pulled skill in-sync.
func skillFilesFor(s remoteSkill) map[string]string {
	if len(s.Files) > 0 {
		files := make(map[string]string, len(s.Files))
		for p, c := range s.Files {
			files[p] = c
		}
		if _, ok := files[skillMainFile]; !ok {
			body := s.Body
			if body == "" {
				body = s.Description
			}
			if body != "" {
				files[skillMainFile] = body
			}
		}
		return files
	}
	content := s.Body
	if content == "" {
		content = s.Description
	}
	return map[string]string{skillMainFile: content}
}

// remoteMcpServer is HQ's structured MCP catalog record (GET /mcp-servers). It is
// a discriminated union on `transport`: `stdio` carries command/args/env;
// `http`/`sse` carry url/headers. Unlike a skill there is NO body field — the
// wrapper reconstructs the on-disk .mcp.json entry entirely from these structured
// fields. Decoded permissively (all fields present, irrelevant ones zero)
// so one struct covers every transport.
type remoteMcpServer struct {
	Name      string            `json:"name"`
	Scope     scope             `json:"scope"`
	Transport string            `json:"transport"`
	Command   string            `json:"command"`
	Args      []string          `json:"args"`
	Env       map[string]string `json:"env"`
	URL       string            `json:"url"`
	Headers   map[string]string `json:"headers"`
}

// remoteWorkflowNode is one node of a workflow DAG: a pointer to a catalog agent
// plus its per-node task, upstream dependency edges (dependsOn), and an optional
// rerun-until-done rule. Mirrors workflowNodeSchema. Decoded as raw structured
// JSON (no markdown body, like remoteMcpServer) — the wrapper re-marshals the
// whole workflow def as the canonical on-disk spec.
type remoteWorkflowNode struct {
	ID        string              `json:"id"`
	Agent     string              `json:"agent"`
	Label     string              `json:"label"`
	Prompt    string              `json:"prompt"`
	DependsOn []string            `json:"dependsOn"`
	Rerun     *remoteWorkflowRule `json:"rerun,omitempty"`
}

// remoteWorkflowRule is a node's rerun-until-done rule: `self` loops until a judge
// declares <endCriteria> met; `declared-by` loops until a checker node declares it
// done. Mirrors workflowRerunSchema. maxRuns is the safety cap.
type remoteWorkflowRule struct {
	Mode        string `json:"mode"`
	EndCriteria string `json:"endCriteria"`
	DeclaredBy  string `json:"declaredBy,omitempty"`
	MaxRuns     int    `json:"maxRuns"`
}

// remoteWorkflow is HQ's structured workflow catalog record (GET /workflows): a
// DAG of catalog agents. Mirrors remoteAgent's envelope (name/scope/kind/
// description) plus the workflow-specific `nodes`. There is NO markdown body — the
// wrapper renders the canonical on-disk spec by re-marshaling these structured
// fields, so a pulled workflow reads back in-sync. `kind` is the
// single literal "workflow" today (the field is kept for catalog parity), so —
// unlike agents/skills — there is no bundle to expand.
type remoteWorkflow struct {
	Name        string               `json:"name"`
	Scope       scope                `json:"scope"`
	Kind        string               `json:"kind"`
	Description string               `json:"description"`
	Nodes       []remoteWorkflowNode `json:"nodes"`
}

// renderWorkflowFile materializes a remoteWorkflow into the canonical on-disk JSON
// spec the wrapper writes at workflows/<name>.json. This is the SAME string the
// wrapper hashes in Fetch, so a freshly pulled workflow reads back in-sync (hash
// parity), mirroring how renderAgentFile produces an agent's body. The struct's
// json tags fix field order/shape deterministically, so two Fetches of the same
// record produce byte-identical bodies.
func renderWorkflowFile(w remoteWorkflow) (string, error) {
	// Force kind to the single literal so a record that omits it (or carries a
	// stale value) still materializes a valid workflow spec.
	if w.Kind == "" {
		w.Kind = string(KindWorkflow)
	}
	b, err := json.MarshalIndent(w, "", "  ")
	if err != nil {
		return "", err
	}
	// Exactly one trailing newline, mirroring renderAgentFile's trailing newline.
	return string(b) + "\n", nil
}

type scope struct {
	Tier string `json:"tier"`
	ID   string `json:"id"`
}

func (s scope) String() string {
	if s.ID == "" {
		return s.Tier
	}
	return s.Tier + "#" + s.ID
}

// remoteProject is the linked project's opt-in, read from GET /projects/{id}.
// Skills/agents are now an org-wide catalog; a project materializes ONLY the
// names it has explicitly opted into. Old project records (lacking these
// fields) decode as nil/empty -> nothing materializes (explicit opt-in).
type remoteProject struct {
	EnabledSkills     []string `json:"enabledSkills"`
	EnabledAgents     []string `json:"enabledAgents"`
	EnabledMcpServers []string `json:"enabledMcpServers"`
	// EnabledBundles names the bundles the project opted into. We expand each
	// bundle's CURRENT members (from the catalog) into the effective skill set at
	// fetch time, so a project never goes stale when a bundle's membership changes
	// after opt-in (the snapshot enabledSkills carries can lag; this self-heals it).
	EnabledBundles []string `json:"enabledBundles"`
	// EnabledAgentBundles names the AGENT bundles the project opted into. Same
	// self-heal: we expand each bundle's current member agents into the effective
	// agent set (and union those agents' skills into the effective skill set) at
	// fetch time, mirroring EnabledBundles for skills.
	EnabledAgentBundles []string `json:"enabledAgentBundles"`
	// EnabledWorkflows names the workflows the project opted into. The wrapper
	// materializes ONLY these names (intersected with the org catalog in Fetch),
	// mirroring EnabledAgents. Workflows have no bundle concept (v1), so there is no
	// companion bundle field to expand.
	EnabledWorkflows []string `json:"enabledWorkflows"`
}

// Fetch reads the org catalog (GET /agents, GET /skills, no ?project, no
// server-side scope resolution) and the linked project's opt-in (GET
// /projects/{id}), then returns the EFFECTIVE set: the intersection of the org
// catalog with project.enabledSkills / project.enabledAgents. Agents' own skills
// are NOT re-expanded here — the backend already union-added each enabled
// agent's skills (bundles flattened) into project.enabledSkills, so they appear
// in the skills intersection naturally. Malformed entries (missing name) are
// skipped rather than failing the whole fetch, so one bad record never blanks
// the meter. A project with empty enabledSkills/enabledAgents yields nothing.
func (h *HTTPRemoteSource) Fetch() ([]RemoteItem, error) {
	// Read the linked project's opt-in first so we can intersect the org catalog
	// against it. Without a project we cannot know what to materialize, so the
	// effective set is empty (explicit opt-in is required).
	enabledSkills := map[string]bool{}
	enabledAgents := map[string]bool{}
	enabledMcp := map[string]bool{}
	enabledBundles := map[string]bool{}
	enabledAgentBundles := map[string]bool{}
	enabledWorkflows := map[string]bool{}
	if h.ProjectID != "" {
		// The REST handler returns the project NESTED under a "project" key
		// (`{project, instances, sessions}`); some shapes (and the unit tests) put
		// the enabled arrays at the top level. Accept BOTH: decode an envelope that
		// carries the flat fields (embedded) AND an optional nested project, and
		// prefer the nested one when present. Without this, a real GET /projects/{id}
		// response reads as empty -> nothing materializes (the bug that made a
		// successful sync still pull 0).
		var env struct {
			remoteProject
			Project *remoteProject `json:"project"`
		}
		if err := h.getJSON("/projects/"+url.PathEscape(h.ProjectID), &env); err != nil {
			return nil, err
		}
		proj := env.remoteProject
		if env.Project != nil {
			proj = *env.Project
		}
		for _, n := range proj.EnabledSkills {
			enabledSkills[n] = true
		}
		for _, n := range proj.EnabledAgents {
			enabledAgents[n] = true
		}
		for _, n := range proj.EnabledMcpServers {
			enabledMcp[n] = true
		}
		for _, n := range proj.EnabledBundles {
			enabledBundles[n] = true
		}
		for _, n := range proj.EnabledAgentBundles {
			enabledAgentBundles[n] = true
		}
		for _, n := range proj.EnabledWorkflows {
			enabledWorkflows[n] = true
		}
	}

	// Build the body cache locally, then publish it under the lock in one shot so a
	// concurrent Body() reader never observes a half-populated map.
	bodies := map[string]string{}
	agentSkills := map[string][]string{}
	var out []RemoteItem
	orgID := ""

	var agentsResp struct {
		Agents []remoteAgent `json:"agents"`
	}
	if err := h.getJSON("/agents", &agentsResp); err != nil {
		return nil, err
	}
	// Expand each ENABLED agent bundle's CURRENT members into the effective agent
	// set, and union each member agent's skills into the effective skill set. Like
	// the skill-bundle expansion below, this self-heals a stale `enabledAgents`
	// snapshot when a bundle's membership changes after opt-in. A bundle agent is
	// never materialized itself (skipped in the loop below); only its members are.
	if len(enabledAgentBundles) > 0 {
		byName := make(map[string]remoteAgent, len(agentsResp.Agents))
		for _, a := range agentsResp.Agents {
			byName[a.Name] = a
		}
		for _, a := range agentsResp.Agents {
			if a.Kind != "bundle" || !enabledAgentBundles[a.Name] {
				continue
			}
			members := a.ResolvedMembers
			if len(members) == 0 {
				members = a.Members
			}
			for _, m := range members {
				enabledAgents[m] = true
				// Bring the member agent's own skill dependencies into the effective
				// skill set so they materialize (mirrors the backend union-on-enable).
				if member, ok := byName[m]; ok {
					for _, s := range member.Skills {
						enabledSkills[s] = true
					}
				}
			}
		}
	}
	for _, a := range agentsResp.Agents {
		if a.Name == "" {
			continue
		}
		// A bundle is a grouping record (no subagent file to materialize); its
		// members are expanded above. Never write a bundle to disk as an agent.
		if a.Kind == "bundle" {
			continue
		}
		if a.Scope.Tier == "org" && a.Scope.ID != "" {
			orgID = a.Scope.ID
		}
		// Effective set: keep only agents the project has opted into.
		if !enabledAgents[a.Name] {
			continue
		}
		// Materialize the full subagent file (frontmatter + prompt) and hash THAT
		// same string, so the body ApplyPulled writes reads back in-sync (parity).
		rendered := renderAgentFile(a)
		bodies[string(KindAgent)+"/"+a.Name] = rendered
		// Record the agent's skill dependencies so Reconcile can ensure they are
		// materialized after the agent.
		if len(a.Skills) > 0 {
			agentSkills[a.Name] = append([]string(nil), a.Skills...)
		}
		out = append(out, RemoteItem{Kind: KindAgent, Name: a.Name, Scope: a.Scope.String(), Hash: hashContent([]byte(rendered))})
	}

	var skillsResp struct {
		Skills []remoteSkill `json:"skills"`
	}
	if err := h.getJSON("/skills", &skillsResp); err != nil {
		return nil, err
	}
	// Expand each ENABLED bundle's CURRENT members into the effective skill set.
	// The project's `enabledSkills` is a snapshot frozen when the bundle was opted
	// into; if the bundle's membership later changes, that snapshot goes stale.
	// Re-flattening the live bundle record here (kind "bundle", carrying the
	// backend-annotated `resolvedMembers`) self-heals it every sync — so a project
	// on `command-hq-starter` always gets the current hq-* set, no re-opt-in needed.
	if len(enabledBundles) > 0 {
		for _, s := range skillsResp.Skills {
			if s.Kind != "bundle" || !enabledBundles[s.Name] {
				continue
			}
			members := s.ResolvedMembers
			if len(members) == 0 {
				members = s.Members
			}
			for _, m := range members {
				enabledSkills[m] = true
			}
		}
	}
	for _, s := range skillsResp.Skills {
		if s.Name == "" {
			continue
		}
		if s.Scope.Tier == "org" && s.Scope.ID != "" {
			orgID = s.Scope.ID
		}
		// A bundle is a grouping record (no body to materialize); its members are
		// expanded above. Never write a bundle to disk as a skill.
		if s.Kind == "bundle" {
			continue
		}
		// Effective set: keep only skills the project has opted into (directly or via
		// an enabled bundle/agent, both union-added above / server-side).
		if !enabledSkills[s.Name] {
			continue
		}
		// Resolve the whole skill directory: an explicit `files` map
		// when HQ serves it, else the single SKILL.md body/description. The cached
		// body is the encoded files envelope (a legacy single-file skill is still the
		// raw SKILL.md text), and the hash covers the WHOLE tree — both computed over
		// the same files map so a pulled skill reads back in-sync (sibling-aware).
		files := skillFilesFor(s)
		bodies[string(KindSkill)+"/"+s.Name] = encodeSkillBody(files)
		out = append(out, RemoteItem{Kind: KindSkill, Name: s.Name, Scope: s.Scope.String(), Hash: hashSkillFiles(files)})
	}

	// MCP servers: read the org catalog, intersect with the project's opt-in, and
	// build the canonical .mcp.json entry FROM the structured fields (there is no
	// markdown body to fall back on). The cached "body" is the canonical
	// entry JSON, hashed identically here and on the read side so a pulled server
	// reads back in-sync. A record with an unknown transport is skipped
	// rather than failing the whole fetch (mirrors the missing-name skip above).
	var mcpResp struct {
		McpServers []remoteMcpServer `json:"mcpServers"`
	}
	if err := h.getJSON("/mcp-servers", &mcpResp); err != nil {
		return nil, err
	}
	for _, m := range mcpResp.McpServers {
		if m.Name == "" {
			continue
		}
		if m.Scope.Tier == "org" && m.Scope.ID != "" {
			orgID = m.Scope.ID
		}
		if !enabledMcp[m.Name] {
			continue
		}
		entry, err := entryForRemote(m)
		if err != nil {
			continue
		}
		canon, err := canonicalEntry(entry)
		if err != nil {
			continue
		}
		bodies[string(KindMcp)+"/"+m.Name] = string(canon)
		out = append(out, RemoteItem{Kind: KindMcp, Name: m.Name, Scope: m.Scope.String(), Hash: hashContent(canon)})
	}

	// Workflows: read the org catalog, intersect with the project's opt-in, and
	// render the canonical spec FROM the structured record (there is no markdown
	// body to fall back on — like MCP servers). The cached "body" is the canonical
	// workflow JSON, hashed identically here and on the read side so a pulled
	// workflow reads back in-sync. A record that fails to render is skipped rather
	// than failing the whole fetch (mirrors the missing-name skip above). Workflows
	// have no bundle concept (v1), so there is no membership expansion.
	var wfResp struct {
		Workflows []remoteWorkflow `json:"workflows"`
	}
	if err := h.getJSON("/workflows", &wfResp); err != nil {
		return nil, err
	}
	for _, wf := range wfResp.Workflows {
		if wf.Name == "" {
			continue
		}
		if wf.Scope.Tier == "org" && wf.Scope.ID != "" {
			orgID = wf.Scope.ID
		}
		// Effective set: keep only workflows the project has opted into.
		if !enabledWorkflows[wf.Name] {
			continue
		}
		// Render the canonical JSON spec and hash THAT same string, so the body
		// ApplyPulled writes reads back in-sync (parity, mirroring agents).
		body, err := renderWorkflowFile(wf)
		if err != nil {
			continue
		}
		bodies[string(KindWorkflow)+"/"+wf.Name] = body
		out = append(out, RemoteItem{Kind: KindWorkflow, Name: wf.Name, Scope: wf.Scope.String(), Hash: hashContent([]byte(body))})
	}

	// Snapshot the FULL declared enabled skill set (enabledSkills after bundle
	// expansion) so the verify gate can assert every declared name materialized —
	// not just the subset that resolved to a catalog record above. A declared name
	// with no leaf in `out` is exactly the silent gap this catches.
	declared := make([]string, 0, len(enabledSkills))
	for n := range enabledSkills {
		declared = append(declared, n)
	}
	sort.Strings(declared)

	// Snapshot the FULL declared enabled agent set (enabledAgents after agent-bundle
	// expansion) — the agent analog of `declared` above, for the verify gate.
	declaredAgents := make([]string, 0, len(enabledAgents))
	for n := range enabledAgents {
		declaredAgents = append(declaredAgents, n)
	}
	sort.Strings(declaredAgents)

	h.bodiesMu.Lock()
	h.bodies = bodies
	h.orgID = orgID
	h.agentSkills = agentSkills
	h.declaredSkills = declared
	h.declaredAgents = declaredAgents
	h.bodiesMu.Unlock()
	return out, nil
}

// DeclaredSkills implements RemoteSource.DeclaredSkills from the snapshot of
// the most recent Fetch. A source not yet fetched yields nil.
func (h *HTTPRemoteSource) DeclaredSkills() []string {
	h.bodiesMu.RLock()
	defer h.bodiesMu.RUnlock()
	return append([]string(nil), h.declaredSkills...)
}

// DeclaredAgents implements RemoteSource.DeclaredAgents from the snapshot of
// the most recent Fetch. A source not yet fetched yields nil.
func (h *HTTPRemoteSource) DeclaredAgents() []string {
	h.bodiesMu.RLock()
	defer h.bodiesMu.RUnlock()
	return append([]string(nil), h.declaredAgents...)
}

// AgentSkills returns the skill names the named agent depends on, captured
// during the most recent Fetch. Unknown agents (or a source not yet fetched)
// yield nil.
func (h *HTTPRemoteSource) AgentSkills(agentName string) []string {
	h.bodiesMu.RLock()
	defer h.bodiesMu.RUnlock()
	return append([]string(nil), h.agentSkills[agentName]...)
}

// Body returns the content captured during the most recent Fetch.
func (h *HTTPRemoteSource) Body(item RemoteItem) (string, error) {
	h.bodiesMu.RLock()
	b, ok := h.bodies[string(item.Kind)+"/"+item.Name]
	h.bodiesMu.RUnlock()
	if ok {
		return b, nil
	}
	return "", fmt.Errorf("no cached body for %s/%s (fetch first)", item.Kind, item.Name)
}

// Push uploads a local-only item to HQ at ORG scope. The catalog is org-only now
// (the 3-tier scope is retired for skills+agents), so a local-authored item is
// published into the org catalog. Pushing a new definition is a deliberate
// authoring action, so the payload is intentionally minimal (name +
// prompt/description at org scope); richer fields are edited in the HQ editor
// The org id is learned from the catalog during Fetch.
func (h *HTTPRemoteSource) Push(item Item, body string) error {
	h.bodiesMu.RLock()
	org := h.orgID
	h.bodiesMu.RUnlock()
	scope := orgScopeFor(org)
	var path string
	var payload any
	switch item.Kind {
	case KindAgent:
		path = "/agents"
		// The local agent file now carries YAML frontmatter (renderAgentFile's
		// output). Parse it back into structured fields and send `prompt` = the
		// body WITHOUT frontmatter, so the next materialize does not double-wrap.
		// Fall back to the on-disk name and "inherit" model when the frontmatter
		// omits them (legacy bare-prompt files parse to empty fields + full body).
		_, description, model, tools, promptBody := parseAgentFile(body)
		if model == "" {
			model = "inherit"
		}
		agentPayload := map[string]any{
			"name":        item.Name,
			"scope":       scope,
			"prompt":      promptBody,
			"description": description,
			"model":       model,
		}
		if len(tools) > 0 {
			agentPayload["tools"] = tools
		}
		payload = agentPayload
	case KindSkill:
		path = "/skills"
		payload = map[string]any{"name": item.Name, "scope": scope, "kind": "skill", "description": body}
	case KindWorkflow:
		// The local workflow file is the canonical JSON spec (renderWorkflowFile's
		// output). Parse it back into structured fields and author the minimal
		// payload to /workflows at org scope, mirroring the agent push. We send the
		// structured nodes (not the whole file verbatim) so the next materialize
		// re-renders identically and does not double-wrap.
		var wf remoteWorkflow
		if err := json.Unmarshal([]byte(body), &wf); err != nil {
			return fmt.Errorf("decode workflow %q for push: %w", item.Name, err)
		}
		path = "/workflows"
		payload = map[string]any{
			"name":        item.Name,
			"scope":       scope,
			"kind":        "workflow",
			"description": wf.Description,
			"nodes":       wf.Nodes,
		}
	case KindMcp:
		// MCP items are NOT one-file-per-item: Reconcile's needs_push branch hands
		// us the WHOLE .mcp.json (os.ReadFile(it.Path)), not a single entry. Extract
		// just this server's entry by name and author the minimal structured payload
		// to /mcp-servers — never push the whole file.
		p, mcpPayload, err := h.mcpPushPayload(item.Name, body, scope)
		if err != nil {
			return err
		}
		path, payload = p, mcpPayload
	default:
		return fmt.Errorf("unknown kind %q", item.Kind)
	}
	return h.postJSON(path, payload)
}

// mcpPushPayload extracts a single server entry from a whole .mcp.json body and
// builds the minimal structured authoring payload for POST /mcp-servers, mapping
// the on-disk `type` discriminator back to the catalog `transport`. Returns an
// error if the file is malformed or the named server is absent.
func (h *HTTPRemoteSource) mcpPushPayload(name, fileBody string, scope map[string]string) (string, any, error) {
	mf, err := decodeMcpFile([]byte(fileBody))
	if err != nil {
		return "", nil, fmt.Errorf("decode .mcp.json for push %q: %w", name, err)
	}
	raw, ok := mf.McpServers[name]
	if !ok {
		return "", nil, fmt.Errorf("mcp server %q not found in .mcp.json", name)
	}
	var entry mcpEntry
	if err := json.Unmarshal(raw, &entry); err != nil {
		return "", nil, fmt.Errorf("decode mcp entry %q: %w", name, err)
	}
	payload := map[string]any{"name": name, "scope": scope, "transport": entry.Type}
	switch entry.Type {
	case "stdio":
		payload["command"] = entry.Command
		payload["args"] = entry.Args
		payload["env"] = entry.Env
	case "http", "sse":
		payload["url"] = entry.URL
		payload["headers"] = entry.Headers
	default:
		return "", nil, fmt.Errorf("unknown mcp type %q for %q", entry.Type, name)
	}
	return "/mcp-servers", payload, nil
}

// CandidateLearnings fetches a skill's CORROBORATED candidate learnings from HQ
// (GET /skills/{name}/candidate-learnings) — the corroborated-only, ranked and
// capped set the daemon injects into the materialized SKILL.md. The
// corroboration gate is enforced server-side; the wrapper renders whatever the
// endpoint returns, in order. Any non-2xx response (including a 404 from an HQ
// that has not deployed the endpoint) is returned as an error;
// InjectCandidateLearnings treats it as non-fatal so surfacing never blocks a
// session or the sync.
func (h *HTTPRemoteSource) CandidateLearnings(name string) ([]candidateLearning, error) {
	var resp struct {
		Learnings []candidateLearning `json:"learnings"`
	}
	path := "/skills/" + url.PathEscape(name) + "/candidate-learnings"
	if err := h.getJSON(path, &resp); err != nil {
		return nil, err
	}
	return resp.Learnings, nil
}

// InjectCandidateLearnings materializes the candidate-learnings block into the
// on-disk SKILL.md of each named skill under `plus`. For every skill it
// fetches the corroborated learnings and rewrites the block in place; an empty set
// removes any prior block (injects nothing). The block is excluded from the drift
// hash (skillFilesCanonical strips it), so this never registers as drift / forces a
// re-pull. Inject is called for EVERY materialized skill regardless of how it was
// enabled (direct/bundle/agent-dep) — the caller passes the full materialized set.
//
// Best-effort: a per-skill fetch or write error is collected and returned but never
// aborts the loop, so one unreachable endpoint or one missing file can't block the
// rest of the session sync. A skill with no on-disk SKILL.md is skipped (it was not
// materialized).
func (h *HTTPRemoteSource) InjectCandidateLearnings(plus string, names []string) []error {
	var errs []error
	for _, name := range names {
		learnings, err := h.CandidateLearnings(name)
		if err != nil {
			errs = append(errs, fmt.Errorf("candidate-learnings fetch %q: %w", name, err))
			continue
		}
		if err := injectCandidateBlock(plus, name, learnings); err != nil {
			errs = append(errs, fmt.Errorf("candidate-learnings inject %q: %w", name, err))
		}
	}
	return errs
}

// getJSON issues an authenticated GET against the org catalog. No ?project
// query param is appended: the catalog is org-wide; the project's opt-in is
// read separately via GET /projects/{id} and applied client-side in Fetch.
func (h *HTTPRemoteSource) getJSON(path string, dst any) error {
	return DoJSON(h.Client, http.MethodGet, h.BaseURL+path, h.Token, nil, dst, path)
}

func (h *HTTPRemoteSource) postJSON(path string, payload any) error {
	return DoJSON(h.Client, http.MethodPost, h.BaseURL+path, h.Token, payload, nil, path)
}

// DoJSON issues one JSON-over-HTTP request: payload (when non-nil) is marshaled
// as the request body, a non-empty token is sent as a Bearer authorization
// header, and a 2xx response body is decoded into dst (or drained when dst is
// nil). A non-2xx status yields an error of the form "<METHOD> <errPath>:
// <status>" — or just the bare status when errPath is "", for callers that wrap
// the error with their own context. It is the single HTTP helper behind the
// catalog source, the memory reconcile, and the CLI login/workflow clients.
func DoJSON(client *http.Client, method, url, token string, payload, dst any, errPath string) error {
	var rdr io.Reader
	if payload != nil {
		b, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("content-type", "application/json")
	if token != "" {
		req.Header.Set("authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()
	if resp.StatusCode/100 != 2 {
		if errPath == "" {
			return fmt.Errorf("%s", resp.Status)
		}
		return fmt.Errorf("%s %s: %s", method, errPath, resp.Status)
	}
	if dst == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(dst)
}
