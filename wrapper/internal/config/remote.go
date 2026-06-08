package config

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
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
// local ones. This is the "remote half" that makes the drift meter real — before
// it, no caller ever fetched HQ state, so drift was always 0.
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

	// bodies caches the content fetched during Fetch so Body() (used by a
	// subsequent reconcile pull) does not re-hit the network. bodiesMu guards it:
	// Fetch (writer) and Body (reader) can run on different goroutines — the drift
	// poll loop and a per-session reconcile both share one source (#12).
	bodiesMu sync.RWMutex
	bodies   map[string]string

	// orgID is the catalog's org, learned from the org scope HQ stamps on every
	// returned skill/agent during Fetch. Push uses it so a local-only item lands
	// at the same org scope HQ serves back (org-only catalog). Guarded by
	// bodiesMu alongside bodies since both are published by Fetch.
	orgID string

	// agentSkills maps an enabled agent's name to the skill names it depends on
	// (agentSchema.skills), captured during Fetch so Reconcile can ensure each dep
	// skill is materialized after the agent (U-Agent-Deps). Guarded by bodiesMu
	// alongside bodies since both are published by Fetch.
	agentSkills map[string][]string
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
	Name string `json:"name"`
	Scope scope `json:"scope"`
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
	// present in <root>/skills, pulling any that are missing (U-Agent-Deps) — an
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
// renderAgentFile and exists for the Push round-trip: a local agent file now
// carries frontmatter, so Push must NOT send the whole thing as `prompt` (that
// would double-wrap on the next materialize). The wrapper has no other YAML
// frontmatter parser (skills push their body verbatim), and adding a YAML
// dependency for four flat string/list fields is overkill — so this is a tiny
// purpose-built parser for exactly the keys renderAgentFile writes.
//
// If the file has no leading `---` frontmatter block (e.g. a legacy bare-prompt
// agent authored before this change), the whole text is returned as the body
// with empty structured fields — that prompt still round-trips losslessly.
func parseAgentFile(content string) (name, description, model string, tools []string, body string) {
	// Normalize CRLF so a Windows-authored file parses identically (hashContent
	// already normalizes line endings for hashing).
	norm := strings.ReplaceAll(content, "\r\n", "\n")
	if !strings.HasPrefix(norm, "---\n") {
		return "", "", "", nil, content
	}
	rest := norm[len("---\n"):]
	end := strings.Index(rest, "\n---\n")
	if end < 0 {
		// Tolerate a frontmatter block that ends the file with no body (and no
		// trailing newline after the closing ---).
		if strings.HasSuffix(rest, "\n---") {
			end = len(rest) - len("\n---")
			body = ""
		} else {
			// No closing fence: treat the whole thing as body (malformed; don't lose it).
			return "", "", "", nil, content
		}
	} else {
		body = rest[end+len("\n---\n"):]
	}
	fm := rest[:end]
	for _, line := range strings.Split(fm, "\n") {
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		val = strings.TrimSpace(val)
		switch key {
		case "name":
			name = val
		case "description":
			description = val
		case "model":
			model = val
		case "tools":
			if val != "" {
				for _, t := range strings.Split(val, ",") {
					if t = strings.TrimSpace(t); t != "" {
						tools = append(tools, t)
					}
				}
			}
		}
	}
	return name, description, model, tools, body
}

type remoteSkill struct {
	Name        string `json:"name"`
	Scope       scope  `json:"scope"`
	Description string `json:"description"`
	// Body is the full SKILL.md text HQ serves on GET /skills and
	// GET /skills/{name}. When present it is the authoritative SKILL.md content to
	// materialize locally; older HQ responses omit it, so we fall back to
	// Description for hashing and pulling to stay backward compatible.
	Body string `json:"body"`
	// Files is the WHOLE skill directory tree (U-Skill-Dirs): relative path within
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
// fields (KTD1). Decoded permissively (all fields present, irrelevant ones zero)
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
	}

	// Build the body cache locally, then publish it under the lock in one shot so a
	// concurrent Body() reader never observes a half-populated map (#12).
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
	for _, a := range agentsResp.Agents {
		if a.Name == "" {
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
		// materialized after the agent (U-Agent-Deps).
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
	for _, s := range skillsResp.Skills {
		if s.Name == "" {
			continue
		}
		if s.Scope.Tier == "org" && s.Scope.ID != "" {
			orgID = s.Scope.ID
		}
		// Effective set: keep only skills the project has opted into. Skills brought
		// by an enabled agent are already present here (union-added server-side).
		if !enabledSkills[s.Name] {
			continue
		}
		// Resolve the whole skill directory (U-Skill-Dirs): an explicit `files` map
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
	// markdown body to fall back on — KTD1). The cached "body" is the canonical
	// entry JSON, hashed identically here and on the read side so a pulled server
	// reads back in-sync (Risk R2). A record with an unknown transport is skipped
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

	h.bodiesMu.Lock()
	h.bodies = bodies
	h.orgID = orgID
	h.agentSkills = agentSkills
	h.bodiesMu.Unlock()
	return out, nil
}

// AgentSkills returns the skill names the named agent depends on, captured during
// the most recent Fetch (U-Agent-Deps). Unknown agents (or a source not yet
// fetched) yield nil. Used by Reconcile to ensure an agent's skills are present
// after the agent is materialized.
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
// (U17). The org id is learned from the catalog during Fetch.
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
	case KindMcp:
		// MCP items are NOT one-file-per-item: Reconcile's needs_push branch hands
		// us the WHOLE .mcp.json (os.ReadFile(it.Path)), not a single entry. Extract
		// just this server's entry by name and author the minimal structured payload
		// to /mcp-servers — never push the whole file (Risk R2).
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

// getJSON issues an authenticated GET against the org catalog. It no longer
// appends a ?project query param: the catalog is org-wide and server-side scope
// resolution is retired; the project's opt-in is read separately via
// GET /projects/{id} and applied client-side in Fetch.
func (h *HTTPRemoteSource) getJSON(path string, dst any) error {
	u := h.BaseURL + path
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	h.auth(req)
	resp, err := h.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("GET %s: %s", path, resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(dst)
}

func (h *HTTPRemoteSource) postJSON(path string, payload any) error {
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodPost, h.BaseURL+path, strings.NewReader(string(b)))
	if err != nil {
		return err
	}
	req.Header.Set("content-type", "application/json")
	h.auth(req)
	resp, err := h.Client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("POST %s: %s", path, resp.Status)
	}
	return nil
}

func (h *HTTPRemoteSource) auth(req *http.Request) {
	if h.Token != "" {
		req.Header.Set("authorization", "Bearer "+h.Token)
	}
}
