/**
 * Helpers for the embedded compliance block. The `/hq-update-progress` flow can
 * inline a machine-generated compliance report into a requirements doc, fenced by
 * `<!--hq:compliance ...-->` ... `<!--/hq:compliance-->` sentinel comments. Command
 * HQ surfaces that block as its own panel and renders the rest of the doc body
 * without it, so the report is never shown twice.
 */

// First compliance block: opening sentinel (optional version token + whitespace),
// inner content (lazy), closing sentinel. Surrounding whitespace is tolerated.
const COMPLIANCE =
  /<!--\s*hq:compliance(?:\s+[^>]*?)?\s*-->([\s\S]*?)<!--\s*\/hq:compliance\s*-->/;

/**
 * Extract the FIRST `<!--hq:compliance-->` block from `content`.
 *
 * Returns `report` = the block's inner content (trimmed, sentinels removed) and
 * `body` = the original content with the whole block (sentinels included) removed.
 * When no block is present, `report` is null and `body` is the unchanged content.
 */
export function extractCompliance(content: string): {
  report: string | null;
  body: string;
} {
  const match = COMPLIANCE.exec(content);
  if (!match) return { report: null, body: content };
  const report = (match[1] ?? '').trim();
  const body = content.slice(0, match.index) + content.slice(match.index + match[0].length);
  return { report, body };
}
