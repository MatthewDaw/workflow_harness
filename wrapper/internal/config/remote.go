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

// Fetch reads /agents and /skills for the project and turns them into RemoteItems
// with content hashes. Malformed entries (missing name) are skipped rather than
// failing the whole fetch, so one bad record never blanks the meter.
func (h *HTTPRemoteSource) Fetch() ([]RemoteItem, error) {
	// Build the body cache locally, then publish it under the lock in one shot so a
	// concurrent Body() reader never observes a half-populated map (#12).
	bodies := map[string]string{}
	var out []RemoteItem

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

// Push uploads a local-only item to HQ at the caller's user scope. Pushing a new
// definition is a deliberate authoring action, so the payload is intentionally
// minimal (name + prompt/description at user scope); richer fields are edited in
// the HQ agent editor (U17).
func (h *HTTPRemoteSource) Push(item Item, body string) error {
	var path string
	var payload any
	switch item.Kind {
	case KindAgent:
		path = "/agents"
		payload = map[string]any{"name": item.Name, "scope": map[string]string{"tier": "user"}, "prompt": body, "model": "inherit"}
	case KindSkill:
		path = "/skills"
		payload = map[string]any{"name": item.Name, "scope": map[string]string{"tier": "user"}, "kind": "skill", "description": body}
	default:
		return fmt.Errorf("unknown kind %q", item.Kind)
	}
	return h.postJSON(path, payload)
}

func (h *HTTPRemoteSource) getJSON(path string, dst any) error {
	u := h.BaseURL + path
	if h.ProjectID != "" {
		u += "?project=" + url.QueryEscape(h.ProjectID)
	}
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
