import { useEffect, useRef, useState } from 'react';
import { EventsOn } from '../wailsjs/runtime/runtime';
import {
  Envelope,
  Ev,
  KIND_ASSISTANT_MSG,
  KIND_SESSION_RENAME,
  KIND_SESSION_START,
  KIND_STATUS_CHANGE,
  KIND_TOOL_CALL,
  KIND_TOOL_RESULT,
  KIND_USER_MSG,
} from './events';

function describe(e: Ev): string {
  switch (e.kind) {
    case KIND_SESSION_START:
      return `${e.name ?? e.sessionId} started`;
    case KIND_SESSION_RENAME:
      return `renamed → ${e.name ?? ''}`;
    case KIND_TOOL_CALL:
      return `tool ${e.tool ?? ''}`;
    case KIND_TOOL_RESULT:
      return `result ${e.summary ?? ''}`;
    case KIND_USER_MSG:
      return 'user message';
    case KIND_ASSISTANT_MSG:
      return 'assistant message';
    case KIND_STATUS_CHANGE:
      return 'status change';
    default:
      return e.kind;
  }
}

export default function Stream() {
  const [events, setEvents] = useState<Envelope[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const off = EventsOn('stream:event', (env: Envelope) => {
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
        background: '#1b1b1c',
        color: '#bbb',
        padding: 8,
        overflowY: 'auto',
        fontFamily: 'monospace',
        fontSize: 12,
      }}
    >
      <strong style={{ color: '#ddd' }}>Stream</strong>
      {events.length === 0 ? (
        <p style={{ opacity: 0.5 }}>No events yet</p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0, margin: '8px 0' }}>
          {events.map((env) => (
            <li key={env.seq} style={{ padding: '2px 0', borderBottom: '1px solid #2a2a2a' }}>
              <span style={{ color: '#6a9955' }}>{env.event.kind}</span>{' '}
              <span style={{ opacity: 0.7 }}>{describe(env.event)}</span>
            </li>
          ))}
        </ul>
      )}
      <div ref={endRef} />
    </aside>
  );
}
