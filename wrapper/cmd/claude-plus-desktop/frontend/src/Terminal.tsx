import { useEffect, useRef } from 'react';
import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { EventsOn } from '../wailsjs/runtime/runtime';
import { SendInput, Resize } from '../wailsjs/go/main/App';

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
    const term = new XTerm({
      convertEol: false,
      fontFamily: 'monospace',
      fontSize: 13,
      // A visible, blinking block caret so you can always see where you're
      // typing. (An unfocused xterm otherwise renders a hard-to-see hollow
      // caret — we also focus it explicitly below.)
      cursorBlink: true,
      cursorStyle: 'block',
      // Keep generous scrollback so you can scroll up through a session's
      // earlier output; typing/new output snaps back to the live edge.
      scrollback: 10000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    const host = hostRef.current!;
    term.open(host);
    fit.fit();
    term.focus();

    // Re-grab focus on click so the caret stays live after interacting with the
    // surrounding chrome (tabs, panels).
    const refocus = () => term.focus();
    host.addEventListener('mousedown', refocus);

    // Keystrokes -> daemon PTY.
    term.onData((data) => {
      void SendInput(data);
    });

    // PTY output (focused session) -> xterm.
    const offOut = EventsOn('pty:output', (p: { sessId: string; dataB64: string }) => {
      term.write(b64ToBytes(p.dataB64));
    });

    // Send the initial size, then on every container resize.
    const sendSize = () => {
      fit.fit();
      void Resize(term.cols, term.rows);
    };
    sendSize();
    const ro = new ResizeObserver(sendSize);
    ro.observe(host);

    return () => {
      offOut();
      ro.disconnect();
      host.removeEventListener('mousedown', refocus);
      term.dispose();
    };
  }, []);

  return <div ref={hostRef} style={{ width: '100%', height: '100%' }} />;
}
