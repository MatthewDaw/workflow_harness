package daemon

import "testing"

// TestReuseDaemonDecision pins the reuse matrix: a daemon is reusable only when
// it is compatible AND its permission posture satisfies the launch. The headline
// case is the last one — a compatible daemon that is NOT in dangerous mode must
// be rejected for a launch that wants dangerous mode, so the flag is never
// silently dropped.
func TestReuseDaemonDecision(t *testing.T) {
	cases := []struct {
		name                              string
		compatible, want, entryDangerous bool
		reusable                          bool
	}{
		{"incompatible is never reused", false, false, false, false},
		{"incompatible not reused even if dangerous matches", false, true, true, false},
		{"compatible plain launch reuses plain daemon", true, false, false, true},
		{"compatible plain launch reuses dangerous daemon (no downgrade)", true, false, true, true},
		{"compatible dangerous launch reuses dangerous daemon", true, true, true, true},
		{"compatible dangerous launch rejects plain daemon", true, true, false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := reuseDaemon(c.compatible, c.want, c.entryDangerous); got != c.reusable {
				t.Fatalf("reuseDaemon(comp=%v, want=%v, entry=%v) = %v, want %v",
					c.compatible, c.want, c.entryDangerous, got, c.reusable)
			}
		})
	}
}
