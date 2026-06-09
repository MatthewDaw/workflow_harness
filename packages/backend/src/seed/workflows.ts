import { orgScope, workflowSchema, type Workflow } from '@harness/shared';
import type { Repo } from '../db/repo.js';

/**
 * Org-scope seed for the workflows that ship bundled with Command HQ + claude+.
 * Mirrors `seed/agents.ts`'s `buildSeedAgents`/`seedAgents`: a fresh deploy
 * starts with an empty workflow catalog, so the Workflows tab shows nothing
 * until something registers workflows. This seed writes a small starter DAG into
 * HQ so an org sees a non-empty catalog.
 *
 * The builder is pure (no I/O) so it is trivially unit-tested; `seedWorkflows`
 * upserts the records through the existing `Repo.putWorkflow`, which makes the
 * seed idempotent (re-running by key leaves exactly one record per workflow).
 *
 * Unlike agents/skills there is no bundle concept — a workflow is itself the
 * composition unit — so every record is `kind:'workflow'` (no bundle branch).
 */

/** A workflow definition fed to the seed builder. */
export interface SeedWorkflowFile {
  name: string;
  /** Short catalog description rendered on the workflow card. */
  description: string;
  /** The DAG nodes (each parsed/defaulted through `workflowNodeSchema`). */
  nodes: Workflow['nodes'];
}

/**
 * Build the seed `Workflow[]`: one `kind:'workflow'` org-scoped record per file.
 * Each record carries the parsed `{name, description, nodes}` and a `system`
 * authorship stamp. Parsing each through `workflowSchema` applies defaults,
 * validates the DAG (unique ids, resolvable refs, acyclic dependsOn), and guards
 * the shape, so a record here is byte-compatible with what the workflows REST
 * layer reads/writes.
 */
export function buildSeedWorkflows(org: string, files: SeedWorkflowFile[]): Workflow[] {
  const scope = orgScope(org);
  const createdBy = { userId: 'system', name: 'system' } as const;

  // Seeded records are the BASE variant of their name (rev 1, empty repo/user):
  // variantId === baseName === name, version 1 — so a later edit forks/advances
  // cleanly and a re-seed never resets the catalog.
  return files.map((f) =>
    workflowSchema.parse({
      name: f.name,
      scope,
      kind: 'workflow',
      description: f.description,
      nodes: f.nodes,
      createdBy,
      baseName: f.name,
      variantId: f.name,
      version: 1,
    }),
  );
}

/**
 * Upsert the seeded workflows into HQ at org scope. Idempotent: `putWorkflow`
 * overwrites by key, so a second run converges to the same set. Returns the
 * records written.
 */
export async function seedWorkflows(
  repo: Repo,
  org: string,
  files: SeedWorkflowFile[],
): Promise<Workflow[]> {
  const records = buildSeedWorkflows(org, files);
  for (const record of records) {
    await repo.putWorkflow(record);
  }
  return records;
}

/**
 * The starter workflow shipped to every org so the catalog is non-empty out of
 * the box (wired into `seed-all-orgs.mjs`). A small 3-node research DAG over
 * plausible seed agents: locate → analyze, with a self-rerun checker on the
 * synthesis node so it loops until its end-criteria is met.
 *
 * The referenced agents (`codebase-locator`, `codebase-analyzer`,
 * `web-search-researcher`) ship as the bundled `.claude/agents/*.md` set, so the
 * union-on-enable pulls real catalog agents when the workflow is added to a
 * project.
 */
export const STARTER_WORKFLOWS: SeedWorkflowFile[] = [
  {
    name: 'research-and-synthesize',
    description:
      'Locate relevant code, analyze it, and synthesize findings — looping the ' +
      'synthesis step until it covers the question.',
    nodes: [
      {
        id: 'locate',
        agent: 'codebase-locator',
        label: 'Locate',
        prompt: 'Find the files and components relevant to the task.',
        dependsOn: [],
      },
      {
        id: 'analyze',
        agent: 'codebase-analyzer',
        label: 'Analyze',
        prompt: 'Analyze the located files in detail to explain how they work.',
        dependsOn: ['locate'],
      },
      {
        id: 'synthesize',
        agent: 'web-search-researcher',
        label: 'Synthesize',
        prompt: 'Synthesize the analysis with external research into a final answer.',
        dependsOn: ['analyze'],
        rerun: {
          mode: 'self',
          endCriteria: 'The synthesis fully answers the task with no open questions.',
          maxRuns: 5,
        },
      },
    ],
  },
];
