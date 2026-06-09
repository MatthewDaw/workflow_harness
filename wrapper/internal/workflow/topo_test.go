package workflow

import (
	"reflect"
	"testing"
)

// TestTopoSortWaves proves a diamond DAG sorts into the expected waves: the root
// alone, then the two parallel middle nodes (sorted), then the join.
func TestTopoSortWaves(t *testing.T) {
	nodes := []Node{
		{ID: "a"},
		{ID: "b", DependsOn: []string{"a"}},
		{ID: "c", DependsOn: []string{"a"}},
		{ID: "d", DependsOn: []string{"b", "c"}},
	}
	waves, err := TopoSort(nodes)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"a"}, {"b", "c"}, {"d"}}
	if !reflect.DeepEqual(waves, want) {
		t.Fatalf("waves = %v, want %v", waves, want)
	}
}

// TestTopoSortLinear proves a simple chain yields one node per wave in order.
func TestTopoSortLinear(t *testing.T) {
	nodes := []Node{
		{ID: "x", DependsOn: []string{"y"}},
		{ID: "y"},
		{ID: "z", DependsOn: []string{"x"}},
	}
	waves, err := TopoSort(nodes)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"y"}, {"x"}, {"z"}}
	if !reflect.DeepEqual(waves, want) {
		t.Fatalf("waves = %v, want %v", waves, want)
	}
}

// TestTopoSortCycle proves a cycle is rejected with an error rather than silently
// dropping nodes.
func TestTopoSortCycle(t *testing.T) {
	nodes := []Node{
		{ID: "a", DependsOn: []string{"b"}},
		{ID: "b", DependsOn: []string{"a"}},
	}
	if _, err := TopoSort(nodes); err == nil {
		t.Fatal("expected a cycle error, got nil")
	}
}

// TestTopoSortDanglingEdgeSkipped proves an unresolvable dependsOn id is skipped
// (not treated as a cycle): the node still schedules in wave 0.
func TestTopoSortDanglingEdgeSkipped(t *testing.T) {
	nodes := []Node{
		{ID: "only", DependsOn: []string{"ghost"}},
	}
	waves, err := TopoSort(nodes)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := [][]string{{"only"}}
	if !reflect.DeepEqual(waves, want) {
		t.Fatalf("waves = %v, want %v", waves, want)
	}
}
