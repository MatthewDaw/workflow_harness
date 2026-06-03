import { useEffect, useState } from "react";
import { EventsOn } from "../wailsjs/runtime/runtime";

interface Status {
  tokens: number;
  costUsd: number;
  drift: number;
}

export default function StatusBar() {
  const [s, setS] = useState<Status>({ tokens: 0, costUsd: 0, drift: 0 });

  useEffect(() => {
    const off = EventsOn("status:update", (st: Status) => setS(st));
    return () => off();
  }, []);

  return (
    <footer
      style={{
        height: 24,
        background: "#007acc",
        color: "#fff",
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "0 12px",
        fontFamily: "monospace",
        fontSize: 12,
        flexShrink: 0,
      }}
    >
      <span>⛁ {s.tokens.toLocaleString()} tok</span>
      <span>${s.costUsd.toFixed(2)}</span>
      <span title="agents/skills out of sync">⟳ {s.drift} drift</span>
    </footer>
  );
}
