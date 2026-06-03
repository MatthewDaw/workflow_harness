import { useEffect, useState } from "react";
import { EventsOn } from "../wailsjs/runtime/runtime";
import { ListSessions, Focus, NewSession, Rename, CloseSession } from "../wailsjs/go/main/App";
import { daemon } from "../wailsjs/go/models";

export default function Sessions() {
  const [sessions, setSessions] = useState<daemon.SessInfo[]>([]);
  // Which session's name is being edited inline (double-click), and its draft.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    // Seed from the attach ack, then keep updated via the sessions event.
    void ListSessions().then((s) => setSessions(s ?? []));
    const off = EventsOn("sessions:update", (list: daemon.SessInfo[]) => {
      setSessions(list ?? []);
    });
    // Auto-titling renames a session by emitting a session.rename event (not a
    // full session-list push), so patch the matching tab's name when one arrives.
    const offRename = EventsOn("stream:event", (env: { event?: { kind?: string; sessionId?: string; name?: string } }) => {
      const ev = env?.event;
      if (ev?.kind === "session.rename" && ev.sessionId) {
        setSessions((prev) =>
          prev.map((s) => (s.id === ev.sessionId ? { ...s, name: ev.name ?? s.name } : s)),
        );
      }
    });
    return () => {
      off();
      offRename();
    };
  }, []);

  function startEdit(s: daemon.SessInfo) {
    setEditingId(s.id);
    setDraft(s.name || s.id);
  }
  function commitEdit() {
    const id = editingId;
    if (id) {
      const name = draft.trim();
      if (name) void Rename(id, name);
    }
    setEditingId(null);
  }
  function cancelEdit() {
    setEditingId(null);
  }

  return (
    <aside
      style={{
        width: 220,
        background: "#252526",
        color: "#ccc",
        padding: 8,
        overflowY: "auto",
        fontFamily: "sans-serif",
        fontSize: 13,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <strong>Sessions</strong>
        <button onClick={() => void NewSession()} title="New session">
          +
        </button>
      </div>
      {sessions.length === 0 ? (
        <p style={{ opacity: 0.6 }}>No sessions — click + to start one</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "8px 0" }}>
          {sessions.map((s) => (
            <li
              key={s.id}
              onClick={() => void Focus(s.id)}
              style={{
                padding: "4px 6px",
                cursor: "pointer",
                background: s.focused ? "#094771" : "transparent",
                borderRadius: 4,
              }}
            >
              {editingId === s.id ? (
                <input
                  autoFocus
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onClick={(e) => e.stopPropagation()}
                  onBlur={commitEdit}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitEdit();
                    else if (e.key === "Escape") cancelEdit();
                  }}
                  style={{
                    width: "70%",
                    background: "#1e1e1e",
                    color: "#fff",
                    border: "1px solid #007acc",
                    borderRadius: 3,
                    font: "inherit",
                    padding: "0 4px",
                  }}
                />
              ) : (
                <span
                  title="Double-click to rename"
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    startEdit(s);
                  }}
                >
                  {s.name || s.id}
                </span>
              )}{" "}
              <small style={{ opacity: 0.6 }}>{s.status}</small>
              <span
                role="button"
                title="Close session"
                onClick={(e) => {
                  e.stopPropagation();
                  void CloseSession(s.id);
                }}
                style={{
                  float: "right",
                  opacity: 0.6,
                  cursor: "pointer",
                  padding: "0 2px",
                }}
              >
                ✕
              </span>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
