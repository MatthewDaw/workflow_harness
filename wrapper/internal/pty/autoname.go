// Package pty hosts the real `claude` CLI children under creack/pty, multiplexes
// them within a single daemon, switches focus, auto-names sessions from the
// first user turn, and propagates SIGWINCH to the focused PTY.
package pty

import (
	"regexp"
	"strings"
)

var (
	nonWord    = regexp.MustCompile(`[^a-z0-9]+`)
	trimDashes = regexp.MustCompile(`^-+|-+$`)
)

// stopWords are dropped from auto-generated names so the slug stays meaningful.
var stopWords = map[string]bool{
	"the": true, "a": true, "an": true, "to": true, "of": true, "for": true,
	"and": true, "or": true, "in": true, "on": true, "with": true, "please": true,
	"can": true, "you": true, "i": true, "want": true, "need": true, "add": true,
}

// Slug converts free text (a first user turn) into a stable, filesystem- and
// tab-safe session name. Limited to maxWords meaningful words.
func Slug(text string, maxWords int) string {
	lower := strings.ToLower(text)
	lower = nonWord.ReplaceAllString(lower, "-")
	lower = trimDashes.ReplaceAllString(lower, "")
	if lower == "" {
		return ""
	}
	parts := strings.Split(lower, "-")
	out := make([]string, 0, maxWords)
	for _, p := range parts {
		if p == "" || stopWords[p] {
			continue
		}
		out = append(out, p)
		if len(out) >= maxWords {
			break
		}
	}
	if len(out) == 0 {
		// Everything was a stop word; fall back to the first few raw words.
		for _, p := range parts {
			if p == "" {
				continue
			}
			out = append(out, p)
			if len(out) >= maxWords {
				break
			}
		}
	}
	return strings.Join(out, "-")
}

// AutoName derives a session name from the first user turn (slugged). Empty
// input yields "" so the caller can fall back to a placeholder.
func AutoName(firstTurn string) string {
	return Slug(firstTurn, 4)
}

// Disambiguate returns name, or name-2, name-3, ... so it does not collide with
// any name already present in taken.
func Disambiguate(name string, taken map[string]bool) string {
	if name == "" {
		name = "session"
	}
	if !taken[name] {
		return name
	}
	for i := 2; ; i++ {
		candidate := name + "-" + itoa(i)
		if !taken[candidate] {
			return candidate
		}
	}
}

// itoa is a tiny dependency-free int->string for the suffix.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
