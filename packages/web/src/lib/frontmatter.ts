/**
 * Tiny YAML-frontmatter helpers for the requirements surfaces. Repo-sourced docs
 * (docs/PRD.md, docs/plans/**) begin with a `---`-delimited frontmatter block
 * carrying `completion:`; we both hide that block from the rendered body and read
 * the number from it so the progress bar matches the doc without a server refresh.
 */

// Leading frontmatter block: optional BOM, `---`, body, closing `---`.
const FRONTMATTER = /^﻿?---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/;

/** Strip a leading `---`-delimited frontmatter block (if any) for display. */
export function stripFrontmatter(markdown: string): string {
  return markdown.replace(FRONTMATTER, '');
}

/**
 * Parse `completion:` (0–100 integer) from a doc's frontmatter, tolerating quotes
 * and a trailing `%` (e.g. `completion: "88%"`). Returns undefined when there is
 * no frontmatter or no completion key.
 */
export function parseCompletion(markdown: string): number | undefined {
  const block = FRONTMATTER.exec(markdown)?.[1];
  if (!block) return undefined;
  const line = block.split(/\r?\n/).find((l) => /^\s*completion\s*:/.test(l));
  if (!line) return undefined;
  const value = line.slice(line.indexOf(':') + 1).replace(/['"%\s]/g, '');
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : undefined;
}
