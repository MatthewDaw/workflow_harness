package pty

import "testing"

func TestSlug(t *testing.T) {
	cases := []struct{ in, want string }{
		{"add reconciliation view", "reconciliation-view"},
		{"Please can you add the reconciliation view", "reconciliation-view"},
		{"  Fix   the   bug!! ", "fix-bug"},
		{"", ""},
		{"!!!", ""},
	}
	for _, c := range cases {
		if got := Slug(c.in, 4); got != c.want {
			t.Errorf("Slug(%q)=%q want %q", c.in, got, c.want)
		}
	}
}

func TestAutoNameFromFirstTurn(t *testing.T) {
	if got := AutoName("add reconciliation view"); got != "reconciliation-view" {
		t.Errorf("first turn slug, got %q", got)
	}
	if got := AutoName(""); got != "" {
		t.Errorf("empty input should yield empty, got %q", got)
	}
}

func TestDisambiguate(t *testing.T) {
	taken := map[string]bool{"build": true, "build-2": true}
	if got := Disambiguate("build", taken); got != "build-3" {
		t.Errorf("want build-3, got %q", got)
	}
	if got := Disambiguate("fresh", taken); got != "fresh" {
		t.Errorf("want fresh, got %q", got)
	}
}
