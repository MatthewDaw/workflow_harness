import type { GitCommit, ProjectFraming } from '@harness/shared';

/**
 * GitHub content parsing + commit attribution (U26).
 *
 * This module is pure: it turns raw repository content (the text of `PRD.md` /
 * `PROGRESS.md`) and raw commit listings into the harness's framing + attributed
 * `GitCommit` shapes. The network is isolated in `app.ts`; everything here is
 * unit-testable against recorded fixtures with no I/O.
 */

/**
 * Parse a project's `PRD.md`. We read the first H1/`Goal:` line as the goal and
 * collect Supporting Outcome ids of the form `SO-…` mentioned under an
 * "Supporting Outcomes"/"Owns" section (or anywhere, as a fallback). Missing
 * content yields an empty framing rather than throwing.
 */
export function parsePrd(md: string | undefined): {
  goal?: string;
  supportingOutcomeIds: string[];
} {
  if (!md || !md.trim()) return { supportingOutcomeIds: [] };

  let goal: string | undefined;
  // Prefer an explicit `Goal:` line; else the first markdown heading's text.
  const goalLine = md.match(/^\s*(?:#+\s*)?goal\s*:\s*(.+)$/im);
  if (goalLine?.[1]) {
    goal = goalLine[1].trim();
  } else {
    const heading = md.match(/^\s*#\s+(.+)$/m);
    if (heading?.[1]) goal = heading[1].trim();
  }

  // Supporting Outcome ids: tokens like SO-12 / SO-NORTH-STAR.
  const soIds = new Set<string>();
  for (const m of md.matchAll(/\b(SO-[A-Z0-9-]+)\b/g)) {
    if (m[1]) soIds.add(m[1]);
  }

  return { goal, supportingOutcomeIds: [...soIds] };
}

/**
 * Parse `PROGRESS.md` for a completion percentage. Accepts `Progress: 42%`,
 * `42% complete`, or a bare `42%`. Returns undefined when no percentage is
 * present.
 */
export function parseProgress(md: string | undefined): number | undefined {
  if (!md) return undefined;
  const m = md.match(/(\d{1,3})\s*%/);
  if (!m?.[1]) return undefined;
  const pct = Number(m[1]);
  if (!Number.isFinite(pct)) return undefined;
  return Math.max(0, Math.min(100, pct));
}

/**
 * Parse a `completion:` percentage from a `docs/plans/` document's YAML
 * frontmatter (U7). Frontmatter is a leading `---` block; we read the
 * `completion:` key (e.g. `completion: 58` or `completion: "58%"`) and clamp it
 * to 0..100. Returns undefined when there is no frontmatter or no `completion:`
 * key so callers can fall back to the legacy `PROGRESS.md %` source.
 */
export function parseCompletionFrontmatter(md: string | undefined): number | undefined {
  if (!md) return undefined;
  // Frontmatter must be the very first thing in the doc: `---\n…\n---`.
  const fm = md.match(/^﻿?---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/);
  if (!fm?.[1]) return undefined;
  const line = fm[1].match(/^[ \t]*completion[ \t]*:[ \t]*["']?(\d{1,3})/im);
  if (!line?.[1]) return undefined;
  const pct = Number(line[1]);
  if (!Number.isFinite(pct)) return undefined;
  return Math.max(0, Math.min(100, pct));
}

/**
 * Resolve a project's progress percentage from the new source of truth (U7):
 * prefer the top `docs/plans/` doc's `completion:` frontmatter; fall back to the
 * legacy `PROGRESS.md %`; finally default to 0. This is the single place the
 * "GitHub is source of truth for progress" precedence is encoded.
 */
export function resolveProgressPct(
  topPlanDoc: string | undefined,
  progressMd: string | undefined,
): number {
  const fromFrontmatter = parseCompletionFrontmatter(topPlanDoc);
  if (fromFrontmatter !== undefined) return fromFrontmatter;
  const fromProgress = parseProgress(progressMd);
  if (fromProgress !== undefined) return fromProgress;
  return 0;
}

/**
 * Assemble the project framing from the two files. Either file being absent
 * (`undefined`) is recorded in `missingFiles` so the UI can show a clear "needs
 * files" state instead of failing.
 */
export function buildFraming(
  prdMd: string | undefined,
  progressMd: string | undefined,
): ProjectFraming {
  const missingFiles: string[] = [];
  if (prdMd === undefined) missingFiles.push('PRD.md');
  if (progressMd === undefined) missingFiles.push('PROGRESS.md');

  const prd = parsePrd(prdMd);
  return {
    goal: prd.goal,
    supportingOutcomeIds: prd.supportingOutcomeIds,
    progressPct: parseProgress(progressMd),
    missingFiles,
  };
}

/** A raw commit as returned by the GitHub commits API (the subset we use). */
export interface RawCommit {
  sha: string;
  commit: {
    message: string;
    author?: { name?: string; date?: string };
  };
  /** Optional branch/ref context, when known from the caller. */
  ref?: string;
}

/**
 * Normalize raw GitHub commits into `GitCommit`s. Used by the Weekly "done"
 * assembly (U28).
 */
export function attributeCommits(raw: RawCommit[]): GitCommit[] {
  return raw.map((c) => ({
    sha: c.sha,
    message: c.commit.message,
    author: c.commit.author?.name ?? '',
    committedAt: c.commit.author?.date ?? '',
  }));
}
