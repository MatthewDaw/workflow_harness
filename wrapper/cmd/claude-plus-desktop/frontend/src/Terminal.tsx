import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { EventsOn } from "../wailsjs/runtime/runtime";
import { SendInput, Resize } from "../wailsjs/go/main/App";

// Decode base64 (the bridge's binary-safe PTY payload) to bytes for xterm.
function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export default function Terminal() {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const term = new XTerm({ convertEol: false, fontFamily: "monospace", fontSize: 13 });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current!);
    fit.fit();

    // Keystrokes -> daemon PTY.
    term.onData((data) => {
      void SendInput(data);
    });

    // PTY output (focused session) -> xterm.
    const offOut = EventsOn("pty:output", (p: { sessId: string; dataB64: string }) => {
      term.write(b64ToBytes(p.dataB64));
    });

    // Send the initial size, then on every container resize.
    const sendSize = () => {
      fit.fit();
      void Resize(term.cols, term.rows);
    };
    sendSize();
    const ro = new ResizeObserver(sendSize);
    ro.observe(hostRef.current!);

    return () => {
      offOut();
      ro.disconnect();
      term.dispose();
    };
  }, []);

  return <div ref={hostRef} style={{ width: "100%", height: "100%" }} />;
}
