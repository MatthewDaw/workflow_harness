import { useEffect, useRef, useState } from "react";
import { EventsOn } from "../wailsjs/runtime/runtime";

// Event/Envelope shapes mirror internal/event (camelCase JSON). Hand-typed
// because Wails generates models for bound-method types, not for event payloads.
interface Ev {
  kind: string;
  sessionId: string;
  name?: string;
  tool?: string;
  summary?: string;
}
interface Envelope {
  ts: number;
  seq: number;
  event: Ev;
}

function describe(e: Ev): string {
  switch (e.kind) {
    case "session.start":
      return `${e.name ?? e.sessionId} started`;
    case "session.rename":
      return `renamed → ${e.name ?? ""}`;
    case "tool.call":
      return `tool ${e.tool ?? ""}`;
    case "tool.result":
      return `result ${e.summary ?? ""}`;
    case "user.msg":
      return "user message";
    case "assistant.msg":
      return "assistant message";
    case "cost.tick":
      return "cost tick";
    case "status.change":
      return "status change";
    default:
      return e.kind;
  }
}

export default function Stream() {
  const [events, setEvents] = useState<Envelope[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const off = EventsOn("stream:event", (env: Envelope) => {
      setEvents((prev) => {
        const next = [...prev, env];
        return next.length > 300 ? next.slice(next.length - 300) : next;
      });
    });
    return () => off();
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView();
  }, [events]);

  return (
    <aside
      style={{
        width: 280,
        background: "#1b1b1c",
        color: "#bbb",
        padding: 8,
        overflowY: "auto",
        fontFamily: "monospace",
        fontSize: 12,
      }}
    >
      <strong style={{ color: "#ddd" }}>Stream</strong>
      {events.length === 0 ? (
        <p style={{ opacity: 0.5 }}>No events yet</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: "8px 0" }}>
          {events.map((env, i) => (
            <li key={i} style={{ padding: "2px 0", borderBottom: "1px solid #2a2a2a" }}>
              <span style={{ color: "#6a9955" }}>{env.event.kind}</span>{" "}
              <span style={{ opacity: 0.7 }}>{describe(env.event)}</span>
            </li>
          ))}
        </ul>
      )}
      <div ref={endRef} />
    </aside>
  );
}
