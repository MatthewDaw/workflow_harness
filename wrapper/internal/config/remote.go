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
	Name   string `json:"name"`
	Scope  scope  `json:"scope"`
	Prompt string `json:"prompt"`
}

type remoteSkill struct {
	Name        string `json:"name"`
	Scope       scope  `json:"scope"`
	Description string `json:"description"`
	// Body is the full SKILL.md text HQ now serves on GET /skills and
	// GET /skills/{name}. When present it is the authoritative content to
	// materialize locally (ApplyPulled writes it to skills/<name>/SKILL.md);
	// older HQ responses omit it, so we fall back to Description for hashing and
	// pulling to stay backward compatible.
	Body string `json:"body"`
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
	EnabledSkills []string `json:"enabledSkills"`
	EnabledAgents []string `json:"enabledAgents"`
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
	if h.ProjectID != "" {
		var proj remoteProject
		if err := h.getJSON("/projects/"+url.PathEscape(h.ProjectID), &proj); err != nil {
			return nil, err
		}
		for _, n := range proj.EnabledSkills {
			enabledSkills[n] = true
		}
		for _, n := range proj.EnabledAgents {
			enabledAgents[n] = true
		}
	}

	// Build the body cache locally, then publish it under the lock in one shot so a
	// concurrent Body() reader never observes a half-populated map (#12).
	bodies := map[string]string{}
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
		bodies[string(KindAgent)+"/"+a.Name] = a.Prompt
		out = append(out, RemoteItem{Kind: KindAgent, Name: a.Name, Scope: a.Scope.String(), Hash: hashContent([]byte(a.Prompt))})
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
		// Prefer the full SKILL.md body when HQ serves it (the authoritative local
		// content); fall back to the description for older HQ responses. Hash the
		// same text we will write so a pulled skill reads back in-sync.
		content := s.Body
		if content == "" {
			content = s.Description
		}
		bodies[string(KindSkill)+"/"+s.Name] = content
		out = append(out, RemoteItem{Kind: KindSkill, Name: s.Name, Scope: s.Scope.String(), Hash: hashContent([]byte(content))})
	}

	h.bodiesMu.Lock()
	h.bodies = bodies
	h.orgID = orgID
	h.bodiesMu.Unlock()
	return out, nil
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
		payload = map[string]any{"name": item.Name, "scope": scope, "prompt": body, "model": "inherit"}
	case KindSkill:
		path = "/skills"
		payload = map[string]any{"name": item.Name, "scope": scope, "kind": "skill", "description": body}
	default:
		return fmt.Errorf("unknown kind %q", item.Kind)
	}
	return h.postJSON(path, payload)
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
