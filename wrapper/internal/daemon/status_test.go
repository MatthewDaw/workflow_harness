package daemon

import (
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

func TestStatusAggregatesCostTicks(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	d.PublishEvent(envWith("s", event.CostTick("s", 0.5, 0.5, 100)))
	d.PublishEvent(envWith("s", event.CostTick("s", 0.25, 0.75, 50)))
	st := d.Status()
	if st.Tokens != 150 {
		t.Errorf("tokens = %d, want 150", st.Tokens)
	}
	if st.CostUSD != 0.75 {
		t.Errorf("cost = %v, want 0.75", st.CostUSD)
	}
}

func TestStatusIgnoresNonCostEvents(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	d.PublishEvent(envWith("s", event.UserMsg("s", 999)))
	st := d.Status()
	if st.Tokens != 0 || st.CostUSD != 0 {
		t.Errorf("non-cost event affected meters: %+v", st)
	}
}

func TestSetDrift(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	d.SetDrift(3)
	if got := d.Status().Drift; got != 3 {
		t.Errorf("drift = %d, want 3", got)
	}
}
