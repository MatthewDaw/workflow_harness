package config

import (
	"reflect"
	"testing"
)

// Characterization tests for the frontmatter parsers. parseAgentFile feeds the
// Push round-trip (a behavior change would double-wrap or lose agent bodies),
// so these pin the fence-edge behavior — CRLF input, a "\n---" fence at EOF,
// unterminated blocks, and no-frontmatter input — that the shared
// frontmatter.go helpers must preserve.

func TestParseAgentFileFenceEdges(t *testing.T) {
	t.Run("crlf input parses and body is LF-normalized", func(t *testing.T) {
		in := "---\r\nname: rev\r\ndescription: code reviewer\r\ntools: Read, Grep\r\nmodel: opus\r\n---\r\nDo the thing.\r\n"
		name, desc, model, tools, body := parseAgentFile(in)
		if name != "rev" || desc != "code reviewer" || model != "opus" {
			t.Fatalf("fields = %q %q %q", name, desc, model)
		}
		if !reflect.DeepEqual(tools, []string{"Read", "Grep"}) {
			t.Fatalf("tools = %v", tools)
		}
		if body != "Do the thing.\n" {
			t.Fatalf("body = %q, want LF-normalized %q", body, "Do the thing.\n")
		}
	})

	t.Run("closing fence at EOF without trailing newline yields empty body", func(t *testing.T) {
		name, _, _, _, body := parseAgentFile("---\nname: rev\n---")
		if name != "rev" {
			t.Fatalf("name = %q", name)
		}
		if body != "" {
			t.Fatalf("body = %q, want empty", body)
		}
	})

	t.Run("unterminated frontmatter returns whole content as body", func(t *testing.T) {
		in := "---\nname: rev\nno closing fence"
		name, desc, model, tools, body := parseAgentFile(in)
		if name != "" || desc != "" || model != "" || tools != nil {
			t.Fatalf("malformed input must yield empty fields, got %q %q %q %v", name, desc, model, tools)
		}
		if body != in {
			t.Fatalf("body = %q, want original content", body)
		}
	})

	t.Run("no frontmatter returns content verbatim with CRLF preserved", func(t *testing.T) {
		in := "a bare prompt\r\nsecond line\r\n"
		name, _, _, _, body := parseAgentFile(in)
		if name != "" {
			t.Fatalf("name = %q", name)
		}
		if body != in {
			t.Fatalf("body = %q, want verbatim original (CRLF kept)", body)
		}
	})

	t.Run("empty and whitespace-padded tools entries are dropped", func(t *testing.T) {
		_, _, _, tools, _ := parseAgentFile("---\nname: x\ntools: Read, , Grep ,\n---\np\n")
		if !reflect.DeepEqual(tools, []string{"Read", "Grep"}) {
			t.Fatalf("tools = %v", tools)
		}
	})
}

func TestFrontmatterFieldFenceEdges(t *testing.T) {
	cases := []struct {
		desc    string
		content string
		key     string
		want    string
	}{
		{"crlf input", "---\r\nname: hq-sync\r\n---\r\nbody\r\n", "name", "hq-sync"},
		{"fence at EOF without newline", "---\nname: hq-sync\n---", "name", "hq-sync"},
		{"unterminated block yields empty", "---\nname: hq-sync\n", "name", ""},
		{"no frontmatter yields empty", "name: hq-sync\n", "name", ""},
		{"missing key yields empty", "---\ndescription: d\n---\nbody\n", "name", ""},
	}
	for _, c := range cases {
		if got := frontmatterField(c.content, c.key); got != c.want {
			t.Errorf("%s: frontmatterField = %q, want %q", c.desc, got, c.want)
		}
	}
}

func TestParseMemoryFrontmatterFenceEdges(t *testing.T) {
	t.Run("crlf input parses including nested metadata.type", func(t *testing.T) {
		in := "---\r\nname: infra\r\ndescription: where prod lives\r\nmetadata:\r\n  type: reference\r\n---\r\nbody\r\n"
		name, desc, typ := parseMemoryFrontmatter(in)
		if name != "infra" || desc != "where prod lives" || typ != "reference" {
			t.Fatalf("got %q %q %q", name, desc, typ)
		}
	})

	t.Run("unterminated block is tolerated and scanned to EOF", func(t *testing.T) {
		in := "---\nname: infra\ndescription: d\nmetadata:\n  type: t"
		name, desc, typ := parseMemoryFrontmatter(in)
		if name != "infra" || desc != "d" || typ != "t" {
			t.Fatalf("got %q %q %q", name, desc, typ)
		}
	})

	t.Run("top-level type is ignored; only metadata-nested type counts", func(t *testing.T) {
		_, _, typ := parseMemoryFrontmatter("---\nname: x\ntype: flat\n---\nbody\n")
		if typ != "" {
			t.Fatalf("flat type must be ignored, got %q", typ)
		}
		_, _, typ = parseMemoryFrontmatter("---\nother:\n  type: nested-elsewhere\nmetadata:\n  type: real\n---\n")
		if typ != "real" {
			t.Fatalf("typ = %q, want only metadata.type accepted", typ)
		}
	})

	t.Run("indented name/description are ignored", func(t *testing.T) {
		name, desc, _ := parseMemoryFrontmatter("---\nmetadata:\n  name: nested\n  description: nested\n---\n")
		if name != "" || desc != "" {
			t.Fatalf("nested name/description must be ignored, got %q %q", name, desc)
		}
	})

	t.Run("no frontmatter yields empty fields", func(t *testing.T) {
		name, desc, typ := parseMemoryFrontmatter("just a body\n")
		if name != "" || desc != "" || typ != "" {
			t.Fatalf("got %q %q %q", name, desc, typ)
		}
	})
}
