package tui

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func TestTabSwitching(t *testing.T) {
	m := New("inst-0")
	if m.Active() != TabSession {
		t.Fatalf("default tab should be Session")
	}
	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyTab})
	if next.(Model).Active() != TabTickets {
		t.Errorf("tab should advance to Tickets")
	}
	// Number key jumps directly.
	jumped, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("4")})
	if jumped.(Model).Active() != TabForge {
		t.Errorf("'4' should jump to Forge, got %v", jumped.(Model).Active())
	}
}

func TestStatusLineReflectsFocusedSession(t *testing.T) {
	m := New("matt@mbp/weekly-compass")
	updated, _ := m.Update(SubTabsMsg{Tabs: []SubTab{{ID: "a", Name: "reconcile-variance"}}, Focused: 0})
	withStatus, _ := updated.(Model).Update(StatusMsg{Tokens: 48000, CostUSD: 0.62})
	view := withStatus.(Model).View()
	if !strings.Contains(view, "reconcile-variance") {
		t.Error("status line should show the focused session name")
	}
	if !strings.Contains(view, "$0.62") {
		t.Error("status line should show the cost meter")
	}
}

func TestDegradedTabsStillRenderSession(t *testing.T) {
	m := New("inst-0")
	down, _ := m.Update(DegradedMsg{Down: true})
	mm := down.(Model)
	mm.active = TabTickets
	if !strings.Contains(mm.View(), "HQ unreachable") {
		t.Error("tickets tab should show degraded state when HQ is down")
	}
	mm.active = TabSession
	if strings.Contains(mm.View(), "HQ unreachable") {
		t.Error("Session tab must keep working when HQ is down")
	}
}

func TestAgentsDriftWarningInTabBar(t *testing.T) {
	m := New("inst-0")
	d, _ := m.Update(DriftMsg{Count: 2})
	if !strings.Contains(d.(Model).renderTabBar(), "⚠2") {
		t.Error("Agents tab should show the drift warning count")
	}
}

func TestStreamPauseHaltsScroll(t *testing.T) {
	s := NewStreamTab()
	s.Append("line1")
	s.TogglePause()
	s.Append("line2")
	if strings.Contains(s.View(), "line2") {
		t.Error("paused stream should not append new lines")
	}
}
