/**
 * Pure path→tree grouping for the Detailed Requirements sidebar (U11).
 *
 * Docs come back with full repo paths under `docs/plans/`, e.g.
 *   docs/plans/command-hq-overview.md            → root level
 *   docs/plans/command-hq/01-plan-mapping.md     → folder `command-hq/`
 *
 * We group by the directory path RELATIVE to `docs/plans/`. Files directly
 * under `docs/plans/` stay at the tree root; files in a subdirectory are nested
 * under a folder node labeled by that subfolder path (arbitrary depth). A folder
 * node carries an aggregate completion = the rounded mean of its child docs.
 *
 * The tree is rendered flat (in order) so the scroll-spy can iterate docs in a
 * single list; each node knows its `depth` for indentation.
 */

/** Minimal doc shape this helper needs (mirrors ProjectDoc). */
export interface DocLike {
  path: string;
  title: string;
  completion: number;
}

/** A folder header row in the flattened tree. */
export interface FolderNode {
  kind: 'folder';
  /** Stable key (the relative directory path, e.g. `command-hq`). */
  key: string;
  /** Display label, e.g. `command-hq/`. */
  label: string;
  /** Indentation depth (0 = root). */
  depth: number;
  /** Rounded mean completion of the folder's descendant docs. */
  completion: number;
}

/** A document row in the flattened tree. */
export interface DocNode<T extends DocLike = DocLike> {
  kind: 'doc';
  doc: T;
  /** Indentation depth (0 = root). */
  depth: number;
}

export type TreeNode<T extends DocLike = DocLike> = FolderNode | DocNode<T>;

const PLANS_PREFIX = 'docs/plans/';

/** Strip the `docs/plans/` prefix; return the path unchanged if absent. */
function relativeToPlans(path: string): string {
  return path.startsWith(PLANS_PREFIX) ? path.slice(PLANS_PREFIX.length) : path;
}

/** The directory segments of a doc, relative to `docs/plans/` (empty = root). */
function dirSegments(path: string): string[] {
  const rel = relativeToPlans(path);
  const parts = rel.split('/');
  parts.pop(); // drop the filename
  return parts.filter(Boolean);
}

function mean(nums: number[]): number {
  if (nums.length === 0) return 0;
  return Math.round(nums.reduce((a, b) => a + b, 0) / nums.length);
}

/**
 * Build a flattened, render-ready tree from a flat doc list. Folder nodes are
 * emitted just before their first child, with an aggregate completion. Root-level
 * docs keep their original order; grouped docs are ordered by first appearance of
 * their folder, preserving doc order within a folder.
 */
export function buildDocTree<T extends DocLike>(docs: T[]): TreeNode<T>[] {
  const nodes: TreeNode<T>[] = [];
  // Track which folder paths we've already emitted a header for.
  const emittedFolders = new Set<string>();

  // Precompute descendant completions per folder prefix for aggregates.
  const folderDocs = new Map<string, number[]>();
  for (const doc of docs) {
    const segs = dirSegments(doc.path);
    let prefix = '';
    for (const seg of segs) {
      prefix = prefix ? `${prefix}/${seg}` : seg;
      const arr = folderDocs.get(prefix) ?? [];
      arr.push(doc.completion);
      folderDocs.set(prefix, arr);
    }
  }

  for (const doc of docs) {
    const segs = dirSegments(doc.path);
    let prefix = '';
    for (let depth = 0; depth < segs.length; depth++) {
      const seg = segs[depth] as string;
      prefix = prefix ? `${prefix}/${seg}` : seg;
      if (!emittedFolders.has(prefix)) {
        emittedFolders.add(prefix);
        nodes.push({
          kind: 'folder',
          key: prefix,
          label: `${seg}/`,
          depth,
          completion: mean(folderDocs.get(prefix) ?? []),
        });
      }
    }
    nodes.push({ kind: 'doc', doc, depth: segs.length });
  }

  return nodes;
}
