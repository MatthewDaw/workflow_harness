import type { GitCommit, ProjectFraming } from '@harness/shared';

/**
 * GitHub content parsing + commit attribution (U26).
 *
 * This module is pure: it turns raw repository content (the text of `PRD.md` /
 * `PROGRESS.md`) and raw commit listings into the harness's framing + attributed
 * `GitCommit` shapes. The network is isolated in `app.ts`; everything here is
 * unit-testable against recorded fixtures with no I/O.
 *
 * Linking convention (plan KTD9 / R6): a commit, branch, or PR is attributed to
 * a ticket when it mentions that ticket's id. The id convention is an uppercase
 * project key plus a number, e.g. `WC-37`. We never guess: a commit with no id
 * match is left unattributed (the Weekly "done" view surfaces those separately
 * rather than mis-assigning them).
 */

/** Matches ticket ids like `WC-37`, `HARNESS-1`, anchored to a word boundary. */
export const TICKET_ID_RE = /\b([A-Z][A-Z0-9]+-\d+)\b/g;

/** Extract every distinct ticket id referenced in a blob of text (branch/PR/msg). */
export function extractTicketIds(...texts: (string | undefined)[]): string[] {
  const ids = new Set<string>();
  for (const text of texts) {
    if (!text) continue;
    for (const m of text.matchAll(TICKET_ID_RE)) {
      if (m[1]) ids.add(m[1]);
    }
  }
  return [...ids];
}

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
 * Normalize raw GitHub commits into attributed `GitCommit`s. Ticket ids are
 * parsed from the commit message (and any provided ref). Used by the Weekly
 * "done" assembly (U28).
 */
export function attributeCommits(raw: RawCommit[]): GitCommit[] {
  return raw.map((c) => ({
    sha: c.sha,
    message: c.commit.message,
    author: c.commit.author?.name ?? '',
    committedAt: c.commit.author?.date ?? '',
    ticketIds: extractTicketIds(c.commit.message, c.ref),
  }));
}
