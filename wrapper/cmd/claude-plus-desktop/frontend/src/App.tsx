import { useState } from "react";
import Terminal from "./Terminal";
import Sessions from "./Sessions";
import Stream from "./Stream";
import StatusBar from "./StatusBar";

type View = "session" | "agents" | "stream";

const TABS: { id: View; label: string }[] = [
  { id: "session", label: "Session" },
  { id: "agents", label: "Agents" },
  { id: "stream", label: "Stream" },
];

function App() {
  const [view, setView] = useState<View>("session");

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", margin: 0 }}>
      {/* Top-level view tabs. The session sub-tab row (Sessions) belongs to the
          Session view only — selecting Agents or Stream hides it. */}
      <nav
        style={{
          display: "flex",
          alignItems: "center",
          background: "#1f2228",
          borderBottom: "1px solid #34383f",
          fontFamily: "sans-serif",
          fontSize: 13,
        }}
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setView(t.id)}
            style={{
              padding: "8px 14px",
              background: view === t.id ? "#1e1e1e" : "transparent",
              color: view === t.id ? "#fff" : "#7e8794",
              border: "none",
              borderBottom: view === t.id ? "2px solid #7fb2e6" : "2px solid transparent",
              cursor: "pointer",
              font: "inherit",
            }}
          >
            {t.label}
          </button>
        ))}
        <span style={{ marginLeft: "auto", padding: "0 14px", color: "#7fc99a", fontSize: 12 }}>
          ● HQ linked
        </span>
      </nav>

      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {/* Sessions sub-tab row — Session view only. */}
        {view === "session" && <Sessions />}

        {/* Terminal stays mounted (hidden off-view) so live PTY output isn't
            lost while you're on Agents/Stream. */}
        <main
          style={{
            flex: 1,
            minWidth: 0,
            background: "#1e1e1e",
            display: view === "session" ? "block" : "none",
          }}
        >
          <Terminal />
        </main>

        {view === "agents" && <Agents />}

        {/* Stream stays mounted (hidden off-view) so it keeps accumulating
            events; shown as the main content on the Stream view. */}
        <div style={{ flex: 1, minWidth: 0, display: view === "stream" ? "flex" : "none" }}>
          <Stream />
        </div>
      </div>

      <StatusBar />
    </div>
  );
}

// Agents is an instance-scoped roster view (no per-session sub-tabs). Placeholder
// until the roster UI lands; the requirement it satisfies here is that selecting
// it hides the session sub-tab row.
function Agents() {
  return (
    <div style={{ flex: 1, color: "#bbb", padding: 16, fontFamily: "sans-serif" }}>
      <h3 style={{ marginTop: 0 }}>Agents</h3>
      <p style={{ opacity: 0.6 }}>
        Agent roster (from <code>~/.claude/agents</code>) — coming soon.
      </p>
    </div>
  );
}

export default App;
