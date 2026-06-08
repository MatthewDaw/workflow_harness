package config

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// TestReadLocalMemoriesParses proves ReadLocalMemories reads a memory/<slug>.md
// file's frontmatter name/description/NESTED metadata.type, sets Content to the
// FULL raw file text, skips the MEMORY.md index, and defaults the name to the
// filename when the frontmatter omits it.
func TestReadLocalMemoriesParses(t *testing.T) {
	dir := t.TempDir()

	// A fully-specified memory: name + description + metadata.type nested block.
	full := "---\nname: deployed-infra\ndescription: where prod lives\nmetadata:\n  type: reference\n---\nThe real DB is DynamoDB.\n"
	writeFile(t, filepath.Join(dir, "deployed-infra.md"), full)

	// A memory with NO name in frontmatter -> name defaults to the filename.
	noName := "---\ndescription: just a note\nmetadata:\n  type: user\n---\nbody here\n"
	writeFile(t, filepath.Join(dir, "scratch.md"), noName)

	// The MEMORY.md index must be skipped (case-insensitive).
	writeFile(t, filepath.Join(dir, "MEMORY.md"), "# index\n- a\n- b\n")
	// A non-.md file must be skipped.
	writeFile(t, filepath.Join(dir, "notes.txt"), "ignore me")

	items, err := ReadLocalMemories(dir)
	if err != nil {
		t.Fatalf("ReadLocalMemories: %v", err)
	}
	byName := map[string]MemoryItem{}
	for _, it := range items {
		byName[it.Name] = it
	}
	if len(byName) != 2 {
		t.Fatalf("got %d memories, want 2 (MEMORY.md + .txt skipped): %+v", len(byName), items)
	}

	di, ok := byName["deployed-infra"]
	if !ok {
		t.Fatalf("missing deployed-infra: %+v", items)
	}
	if di.Description != "where prod lives" {
		t.Errorf("description = %q", di.Description)
	}
	if di.Type != "reference" {
		t.Errorf("nested metadata.type = %q, want reference", di.Type)
	}
	if di.Content != full {
		t.Errorf("Content must be the FULL raw file text, got %q", di.Content)
	}

	sc, ok := byName["scratch"]
	if !ok {
		t.Fatalf("missing filename-defaulted memory: %+v", items)
	}
	if sc.Type != "user" {
		t.Errorf("scratch metadata.type = %q, want user", sc.Type)
	}
	if sc.Description != "just a note" {
		t.Errorf("scratch description = %q", sc.Description)
	}
}

// TestReadLocalMemoriesMissingDir proves a project with no memory/ dir reconciles
// to an empty set (nil slice, nil error) rather than failing the sync.
func TestReadLocalMemoriesMissingDir(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist")
	items, err := ReadLocalMemories(missing)
	if err != nil {
		t.Fatalf("missing dir must not error: %v", err)
	}
	if len(items) != 0 {
		t.Fatalf("missing dir must yield empty set, got %+v", items)
	}
}

// TestReconcileMemoriesPosts proves ReconcileMemories PUTs the expected JSON to
// /projects/{id}/memories with the Bearer token and the {"memories":[...]} body.
func TestReconcileMemoriesPosts(t *testing.T) {
	type captured struct {
		method string
		path   string
		auth   string
		body   struct {
			Memories []MemoryItem `json:"memories"`
		}
	}
	var got captured
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.method = r.Method
		got.path = r.URL.Path
		got.auth = r.Header.Get("Authorization")
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &got.body)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	items := []MemoryItem{
		{Name: "a", Description: "desc-a", Type: "user", Content: "raw a"},
		{Name: "b", Content: "raw b"},
	}
	if err := ReconcileMemories(srv.URL, "tok123", "my-proj", items); err != nil {
		t.Fatalf("ReconcileMemories: %v", err)
	}

	if got.method != http.MethodPut {
		t.Errorf("method = %q, want PUT", got.method)
	}
	if got.path != "/projects/my-proj/memories" {
		t.Errorf("path = %q", got.path)
	}
	if got.auth != "Bearer tok123" {
		t.Errorf("auth = %q", got.auth)
	}
	if len(got.body.Memories) != 2 {
		t.Fatalf("body memories = %d, want 2: %+v", len(got.body.Memories), got.body)
	}
	if got.body.Memories[0].Name != "a" || got.body.Memories[0].Type != "user" || got.body.Memories[0].Content != "raw a" {
		t.Errorf("memory[0] = %+v", got.body.Memories[0])
	}
}

// TestReconcileMemoriesEmptyAndError proves an empty set still sends a concrete
// array (full-reconcile clear) and a non-2xx response surfaces as an error.
func TestReconcileMemoriesEmptyAndError(t *testing.T) {
	var sawArray bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Memories []MemoryItem `json:"memories"`
		}
		b, _ := io.ReadAll(r.Body)
		// Decode then re-check the raw to ensure it's [] (a concrete array), not null.
		_ = json.Unmarshal(b, &body)
		sawArray = body.Memories != nil
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer srv.Close()

	err := ReconcileMemories(srv.URL, "tok", "p", nil)
	if err == nil {
		t.Fatal("non-2xx must return an error")
	}
	if !sawArray {
		t.Fatal("empty/nil set must still send a concrete [] array, not null")
	}
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}
