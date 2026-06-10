import type { Envelope } from '@harness/shared';

/** Short HH:MM:SS clock for an event's epoch-ms timestamp. */
export function clock(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * A rendered feed row: a compact header (who/what) plus an optional multi-line
 * body carrying the REAL content — assistant/user text, tool call name+input,
 * tool result output / console logs. The body is rendered monospace + pre-wrap
 * so console output keeps its shape.
 */
export interface FeedRow {
  header: string;
  body?: string;
}

/** Map one envelope's event to a feed row with full content. */
export function feedRow(env: Envelope): FeedRow | null {
  const e = env.event;
  switch (e.kind) {
    case 'tool.call':
      return { header: `→ ${e.tool}`, body: e.argsSummary || undefined };
    case 'tool.result':
      return { header: `${e.ok ? '✓' : '✗'} ${e.ms}ms`, body: e.summary || undefined };
    case 'user.msg':
      return { header: `▎ you · ${e.tokens} tok`, body: e.text || undefined };
    case 'assistant.msg':
      return { header: `▎ claude · ${e.tokens} tok`, body: e.text || undefined };
    case 'status.change':
      return { header: `● ${e.from} → ${e.to}` };
    case 'session.rename':
      return { header: `✎ renamed → ${e.name}` };
    case 'session.start':
    default:
      return null;
  }
}
