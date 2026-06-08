package config

// MCP server materialization (U-MCP-Target). Where a skill materializes as a
// SKILL.md file and an agent as an agents/<name>.md file, an MCP server
// materializes as one ENTRY merged into ~/.claude+/<root>/.claude.json under the
// `mcpServers` key — a seed-once, claude+-owned per-project file (overlay.go).
//
// SPIKE RESOLUTION (U-MCP-Target): Claude Code reads its MCP servers from
// <root>/.claude.json `mcpServers`, NOT from a `.mcp.json` file (which does not
// exist in real config roots). A file-based server is only LIVE once its name is
// also listed under `enabledMcpjsonServers` (Claude's approval list for
// project/file-declared servers), so the wrapper adds every server it writes to
// that list as well. Both writes MERGE into the existing .claude.json — they never
// clobber the file's many other top-level keys (auth/identity/UI state) nor other
// servers (Risk R4).
//
// This breaks two assumptions the skills path relies on: items are no longer
// one-file-per-item (many servers share one .claude.json), and HQ serves a
// structured record rather than a ready-made body to hash. Drift correctness
// therefore hinges on ONE canonical serialization used on BOTH the read side
// (ReadLocal hashing the on-disk entry) and the apply/fetch side (hashing the
// HQ-built entry): if they differ by even key order or whitespace, every server
// shows perpetual `differs` (Risk R2).
//
// On-disk shape: Claude keys each server under `mcpServers[<name>]` and
// discriminates the entry with a `type` field (NOT `transport`) — `stdio` carries
// command/args/env, `http`/`sse` carry url/headers. mcp.go is the single place
// that maps the catalog record's `transport` to the on-disk `type`.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// mcpFileName is the file each server entry is merged into, under each per-project
// config root. Claude Code reads MCP servers from <root>/.claude.json mcpServers
// (the spike confirmed .mcp.json does not exist in real roots). It is a seed-once,
// claude+-owned per-project file (overlay.go).
const mcpFileName = ".claude.json"

// enabledMcpjsonKey is the top-level .claude.json array of server NAMES Claude
// treats as approved file/project-declared MCP servers. A server written into
// mcpServers is only actually launched once its name appears here, so ApplyPulled
// adds every synced server's name to this list (merging, de-duplicated).
const enabledMcpjsonKey = "enabledMcpjsonServers"

// mcpEntry is one server's on-disk shape in .claude.json mcpServers. Field order here is NOT
// what determines the hash — canonicalEntry re-serializes with sorted keys — but
// the JSON tags pin the on-disk key names. `Type` is the on-disk discriminator
// (Claude uses `type`, not `transport`). omitempty keeps an stdio entry from
// emitting url/headers and an http entry from emitting command/args, so the
// shape Claude reads is minimal and exact.
type mcpEntry struct {
	Type    string            `json:"type"`
	Command string            `json:"command,omitempty"`
	Args    []string          `json:"args,omitempty"`
	Env     map[string]string `json:"env,omitempty"`
	URL     string            `json:"url,omitempty"`
	Headers map[string]string `json:"headers,omitempty"`
}

// mcpFile is the top-level .claude.json document (as far as MCP cares). mcpServers
// holds the per-name entries; Extra captures every OTHER top-level key (auth,
// identity, UI state, and the enabledMcpjsonServers approval list) so a merge
// write preserves them verbatim — we never clobber keys we do not understand.
// enableMcpjsonServer reads/updates enabledMcpjsonServers via Extra.
type mcpFile struct {
	McpServers map[string]json.RawMessage
	Extra      map[string]json.RawMessage
}

// canonicalEntry serializes an mcpEntry to byte-stable JSON: object keys are
// emitted in sorted order at every level (Go's encoding/json already sorts map
// keys, and we sort the struct's fields explicitly by round-tripping through a
// map) so that an entry built from the HQ record hashes identically to the same
// entry read back off disk regardless of source key order. This is the linchpin
// of MCP drift correctness (Risk R2): ReadLocal and Fetch both hash
// canonicalEntry output, and ApplyPulled writes canonicalEntry output, so a
// freshly pulled server reads back in-sync.
func canonicalEntry(e mcpEntry) ([]byte, error) {
	// Marshal the struct (omitempty drops irrelevant per-transport fields), then
	// re-decode into a generic map and re-marshal so output key order is fully
	// determined by encoding/json's lexical map-key sort — independent of struct
	// field order or any incoming key order.
	raw, err := json.Marshal(e)
	if err != nil {
		return nil, err
	}
	return canonicalJSON(raw)
}

// canonicalJSON re-serializes arbitrary JSON with object keys in sorted order at
// every nesting level, producing byte-identical output for inputs that differ
// only in key order or insignificant whitespace. Arrays preserve order (it is
// significant for `args`). Used so the on-disk entry and the HQ-built entry hash
// equal.
func canonicalJSON(raw []byte) ([]byte, error) {
	var v any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	if err := writeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// writeCanonical emits v with sorted object keys. It mirrors encoding/json's
// scalar formatting (via json.Marshal for leaves) but takes control of object
// key ordering so the result is deterministic.
func writeCanonical(buf *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			kb, err := json.Marshal(k)
			if err != nil {
				return err
			}
			buf.Write(kb)
			buf.WriteByte(':')
			if err := writeCanonical(buf, t[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
		return nil
	case []any:
		buf.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
		return nil
	default:
		b, err := json.Marshal(t)
		if err != nil {
			return err
		}
		buf.Write(b)
		return nil
	}
}

// parseMcpFile reads a .claude.json document, splitting the recognized `mcpServers`
// map from every other top-level key (Extra). A missing file is not an error: it
// yields an empty document so callers can merge into it (the seeded file may not
// exist yet, or may be absent in a fresh registry). Malformed JSON IS returned as
// an error so callers can surface it as an Err item rather than panicking or
// silently dropping every other server.
func parseMcpFile(path string) (*mcpFile, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &mcpFile{McpServers: map[string]json.RawMessage{}, Extra: map[string]json.RawMessage{}}, nil
		}
		return nil, err
	}
	return decodeMcpFile(b)
}

// decodeMcpFile parses raw .mcp.json bytes into an mcpFile. An empty/whitespace
// file decodes as an empty document (treated like a missing file). Malformed JSON
// is an error.
func decodeMcpFile(b []byte) (*mcpFile, error) {
	mf := &mcpFile{McpServers: map[string]json.RawMessage{}, Extra: map[string]json.RawMessage{}}
	if len(bytes.TrimSpace(b)) == 0 {
		return mf, nil
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(b, &top); err != nil {
		return nil, err
	}
	for k, v := range top {
		if k == "mcpServers" {
			if len(bytes.TrimSpace(v)) == 0 || string(bytes.TrimSpace(v)) == "null" {
				continue
			}
			if err := json.Unmarshal(v, &mf.McpServers); err != nil {
				return nil, err
			}
			continue
		}
		mf.Extra[k] = v
	}
	if mf.McpServers == nil {
		mf.McpServers = map[string]json.RawMessage{}
	}
	return mf, nil
}

// canonicalServerHash parses one server entry's raw JSON and returns the hash of
// its canonical serialization — the same hash Fetch computes from the HQ record.
// Used by ReadLocal so an on-disk entry compares equal to its HQ-built twin.
func canonicalServerHash(raw json.RawMessage) (string, error) {
	c, err := canonicalJSON(raw)
	if err != nil {
		return "", err
	}
	return hashContent(c), nil
}

// mergeMcpServer reads path (or starts empty), sets mcpServers[name] to entry,
// ensures the name is present in enabledMcpjsonServers (so Claude actually
// launches the file-declared server), and writes the document back — a MERGE,
// never a whole-file overwrite. Other servers under mcpServers and every unrelated
// top-level key (Extra, including auth/identity/UI state in .claude.json) survive
// the write. The written entry uses the canonical serialization so a subsequent
// ReadLocal hashes it identically to the HQ record (idempotent pull).
func mergeMcpServer(path, name string, entry mcpEntry) error {
	mf, err := parseMcpFile(path)
	if err != nil {
		return err
	}
	canon, err := canonicalEntry(entry)
	if err != nil {
		return err
	}
	mf.McpServers[name] = json.RawMessage(canon)
	if err := mf.enableMcpjsonServer(name); err != nil {
		return err
	}
	return writeMcpFile(path, mf)
}

// enableMcpjsonServer adds name to the top-level enabledMcpjsonServers array
// (stored in Extra so writeMcpFile round-trips it), de-duplicated and order-stable.
// A server written into mcpServers is only LIVE once approved here, so every pull
// adds its name. Idempotent: re-adding an already-listed name is a no-op, so a
// repeated pull does not churn the file. An existing value of an unexpected JSON
// shape is replaced with a fresh array containing just name (we never corrupt the
// file, and the server still becomes enabled).
func (mf *mcpFile) enableMcpjsonServer(name string) error {
	var names []string
	if raw, ok := mf.Extra[enabledMcpjsonKey]; ok && len(bytes.TrimSpace(raw)) > 0 {
		// Tolerate a non-array value (or null) by starting fresh.
		_ = json.Unmarshal(raw, &names)
	}
	for _, n := range names {
		if n == name {
			return nil // already enabled — no churn
		}
	}
	names = append(names, name)
	sort.Strings(names)
	b, err := json.Marshal(names)
	if err != nil {
		return err
	}
	mf.Extra[enabledMcpjsonKey] = json.RawMessage(b)
	return nil
}

// writeMcpFile serializes an mcpFile back to disk with stable key order. The
// top-level object is assembled with mcpServers plus all Extra keys; each server
// entry is canonicalized so the on-disk bytes match what ReadLocal will hash.
func writeMcpFile(path string, mf *mcpFile) error {
	top := map[string]json.RawMessage{}
	for k, v := range mf.Extra {
		top[k] = v
	}
	// Canonicalize each server entry so order is stable regardless of how it was
	// parsed/built; build the mcpServers object with sorted server names.
	names := make([]string, 0, len(mf.McpServers))
	for n := range mf.McpServers {
		names = append(names, n)
	}
	sort.Strings(names)
	var servers bytes.Buffer
	servers.WriteByte('{')
	for i, n := range names {
		if i > 0 {
			servers.WriteByte(',')
		}
		nb, err := json.Marshal(n)
		if err != nil {
			return err
		}
		canon, err := canonicalJSON(mf.McpServers[n])
		if err != nil {
			return err
		}
		servers.Write(nb)
		servers.WriteByte(':')
		servers.Write(canon)
	}
	servers.WriteByte('}')
	top["mcpServers"] = json.RawMessage(servers.Bytes())

	out, err := json.MarshalIndent(top, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, out, 0o644)
}

// entryForRemote maps a structured HQ MCP record (decoded into remoteMcpServer)
// to the on-disk mcpEntry, translating the catalog `transport` to the on-disk
// `type` discriminator and selecting the per-transport fields. There is NO
// markdown body fallback — the entry is built entirely from the structured
// fields (KTD1). An unknown transport is an error so a malformed catalog record
// never produces a silently-wrong entry.
func entryForRemote(s remoteMcpServer) (mcpEntry, error) {
	switch s.Transport {
	case "stdio":
		return mcpEntry{
			Type:    "stdio",
			Command: s.Command,
			Args:    s.Args,
			Env:     s.Env,
		}, nil
	case "http", "sse":
		return mcpEntry{
			Type:    s.Transport,
			URL:     s.URL,
			Headers: s.Headers,
		}, nil
	default:
		return mcpEntry{}, fmt.Errorf("unknown mcp transport %q", s.Transport)
	}
}
