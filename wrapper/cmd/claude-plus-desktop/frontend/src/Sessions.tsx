import { useEffect, useState } from "react";
import { EventsOn } from "../wailsjs/runtime/runtime";
import { ListSessions, Focus, NewSession } from "../wailsjs/go/main/App";
import { daemon } from "../wailsjs/go/models";

export default function Sessions() {
  const [sessions, setSessions] = useState<daemon.SessInfo[]>([]);

  useEffect(() => {
    // Seed from the attach ack, then keep updated via the sessions event.
    void ListSessions().then((s) => setSessions(s ?? []));
    const off = EventsOn("sessions:update", (list: daemon.SessInfo[]) => {
      setSessions(list ?? []);
    });
    return () => off();
  }, []);

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
              {s.name || s.id} <small style={{ opacity: 0.6 }}>{s.status}</small>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
