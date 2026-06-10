import type { McpServer } from '@harness/shared';

/** The author/creator of a catalog entry: its createdBy name, or the fallback. */
export function authorOf(x: { createdBy?: { name: string } }, fallback = 'Unknown'): string {
  return x.createdBy?.name ?? fallback;
}

/**
 * Stable sort that leads with bundles (the entry points that drill into their
 * members), keeping the original order within each group so the catalog stays
 * steady.
 */
export function bundlesFirst<T>(items: T[], isBundle: (item: T) => boolean): T[] {
  return items
    .map((x, i) => [x, i] as const)
    .sort(([a, ai], [b, bi]) => {
      const rank = (x: T) => (isBundle(x) ? 0 : 1);
      return rank(a) - rank(b) || ai - bi;
    })
    .map(([x]) => x);
}

/**
 * A one-line, secret-free summary of where an MCP server lives: the spawned
 * command (stdio) or the remote endpoint (http/sse).
 */
export function mcpSummary(s: McpServer): string {
  return s.transport === 'stdio' ? [s.command, ...s.args].join(' ').trim() : s.url;
}
