import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import type { GitCommit, ObjectiveNode, Ticket, WeeklyItem, WeeklyUpdate } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  attributeDone,
  computeDeltas,
  summarizeAlignment,
  type AlignmentDelta,
  type AlignmentSummary,
  type DoneAttribution,
} from './align.js';

/**
 * Weekly Update agent (U28, KTD11).
 *
 * Given a user's stated plan, the agent fetches the company objectives + the
 * project's owned Supporting Outcomes and **pushes back** on any plan item that
 * maps to none of them. On acceptance it assembles "Done" from the week's git
 * commits (U26) attributed to objectives, and "Plan" from the validated
 * commitments, plus alignment deltas + completion (the U10 roll-up inputs).
 *
 * The reasoning is split: the deterministic mapping (does this item link to an
 * owned outcome?) is done here so push-back is testable without an LLM; the LLM
 * (Bedrock) only writes the *prose* of the challenge. Tests inject a fake
 * `Challenger`.
 */

/** Writes the prose challenge for an unaligned plan item. Injectable (Bedrock). */
export interface Challenger {
  challenge(input: { item: WeeklyItem; ownedOutcomes: ObjectiveNode[] }): Promise<string>;
}

/** Bedrock-backed challenger (Claude via the Bedrock Messages API). */
export class BedrockChallenger implements Challenger {
  constructor(
    private readonly client: BedrockRuntimeClient,
    private readonly modelId: string = process.env.BEDROCK_TEXT_MODEL ??
      'anthropic.claude-3-5-sonnet-20240620-v1:0',
  ) {}

  async challenge(input: { item: WeeklyItem; ownedOutcomes: ObjectiveNode[] }): Promise<string> {
    const outcomes = input.ownedOutcomes.map((o) => `- ${o.id}: ${o.title}`).join('\n');
    const res = await this.client.send(
      new InvokeModelCommand({
        modelId: this.modelId,
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify({
          anthropic_version: 'bedrock-2023-05-31',
          max_tokens: 256,
          messages: [
            {
              role: 'user',
              content:
                `This planned work maps to none of the project's owned Supporting Outcomes:\n` +
                `"${input.item.text}"\n\nOwned outcomes:\n${outcomes}\n\n` +
                `Briefly push back: ask which outcome it advances or why it should be done anyway.`,
            },
          ],
        }),
      }),
    );
    const decoded = JSON.parse(new TextDecoder().decode(res.body)) as {
      content?: { text?: string }[];
    };
    return decoded.content?.[0]?.text?.trim() ?? 'Which owned outcome does this advance?';
  }
}

/** A plan item the agent challenged because it maps to no owned outcome. */
export interface Challenge {
  item: WeeklyItem;
  message: string;
}

/** The result of validating a stated plan against owned outcomes. */
export interface ValidationResult {
  /** Items that map to an owned Supporting Outcome — they pass. */
  accepted: WeeklyItem[];
  /** Items that map to none — challenged, excluded unless re-justified. */
  challenges: Challenge[];
}

export interface ValidateDeps {
  challenger: Challenger;
}

/**
 * Validate a stated plan against the project's owned outcomes. An item with an
 * `objectiveId` in `ownedOutcomeIds` is accepted; one without is challenged. An
 * item carrying an explicit `justified: true` (re-justified by the user after a
 * prior round) is accepted even without a link.
 */
export async function validatePlan(
  plan: (WeeklyItem & { justified?: boolean })[],
  ownedOutcomes: ObjectiveNode[],
  deps: ValidateDeps,
): Promise<ValidationResult> {
  const ownedIds = new Set(ownedOutcomes.map((o) => o.id));
  const accepted: WeeklyItem[] = [];
  const challenges: Challenge[] = [];

  for (const item of plan) {
    const linked = item.objectiveId !== undefined && ownedIds.has(item.objectiveId);
    if (linked || item.justified) {
      accepted.push({
        text: item.text,
        objectiveId: item.objectiveId,
        completionPct: item.completionPct,
      });
    } else {
      const message = await deps.challenger.challenge({ item, ownedOutcomes });
      challenges.push({ item, message });
    }
  }

  return { accepted, challenges };
}

/** Inputs for assembling the final weekly update once the plan is accepted. */
export interface AssembleInput {
  projectId: string;
  isoWeek: string;
  ownedOutcomeIds: string[];
  /** The validated plan items (output of `validatePlan`). */
  plan: WeeklyItem[];
  /** The week's commits (from U26 `listCommits`). */
  commits: GitCommit[];
  /** The project's tickets, for commit→objective attribution. */
  tickets: Ticket[];
  /** Objective nodes, for alignment deltas against prior cached %. */
  nodes: ObjectiveNode[];
}

/** The assembled weekly update plus its alignment/attribution breakdowns. */
export interface AssembledWeekly {
  update: WeeklyUpdate;
  doneAlignment: AlignmentSummary;
  planAlignment: AlignmentSummary;
  deltas: AlignmentDelta[];
  attribution: DoneAttribution;
}

/**
 * Assemble the "Done" section from the week's commits attributed to objectives,
 * pair it with the validated "Plan", and compute the alignment + completion
 * breakdowns. A week with no commits yields an empty Done with a noted item
 * rather than failing (plan edge case).
 */
export function assembleWeekly(input: AssembleInput): AssembledWeekly {
  const attribution = attributeDone(input.commits, input.tickets);

  // "Done" items: one per objective the week's commits advanced, plus a note for
  // unattributed commits so they are surfaced, not hidden (plan R6).
  const done: WeeklyItem[] = Object.entries(attribution.byObjective).map(
    ([objectiveId, commits]) => ({
      text: `${commits.length} commit(s) advancing ${objectiveId}`,
      objectiveId,
      completionPct: 100,
    }),
  );
  if (input.commits.length === 0) {
    done.push({ text: 'No commits recorded this week.' });
  } else if (attribution.unattributed.length > 0) {
    done.push({
      text: `${attribution.unattributed.length} unattributed commit(s) (no ticket→objective link).`,
    });
  }

  const update: WeeklyUpdate = {
    projectId: input.projectId,
    isoWeek: input.isoWeek,
    done,
    plan: input.plan,
    validated: true,
  };

  const doneAlignment = summarizeAlignment(done, input.ownedOutcomeIds);
  const planAlignment = summarizeAlignment(input.plan, input.ownedOutcomeIds);
  const deltas = computeDeltas(planAlignment, input.nodes);

  return { update, doneAlignment, planAlignment, deltas, attribution };
}

export interface DraftDeps extends ValidateDeps {
  repo: Repo;
  challenger: Challenger;
}

/**
 * End-to-end draft: validate the stated plan, and (when nothing remains
 * challenged, or all challenges were re-justified) assemble the full weekly
 * update from the gathered commits/tickets/objectives. When challenges remain,
 * the caller surfaces them to the user for a re-justification round before
 * assembling — so this returns `assembled: undefined` in that case.
 */
export async function draftWeekly(
  input: {
    projectId: string;
    isoWeek: string;
    plan: (WeeklyItem & { justified?: boolean })[];
    ownedOutcomes: ObjectiveNode[];
    commits: GitCommit[];
    tickets: Ticket[];
    nodes: ObjectiveNode[];
  },
  deps: DraftDeps,
): Promise<{ validation: ValidationResult; assembled?: AssembledWeekly }> {
  const validation = await validatePlan(input.plan, input.ownedOutcomes, deps);
  if (validation.challenges.length > 0) {
    return { validation };
  }

  const assembled = assembleWeekly({
    projectId: input.projectId,
    isoWeek: input.isoWeek,
    ownedOutcomeIds: input.ownedOutcomes.map((o) => o.id),
    plan: validation.accepted,
    commits: input.commits,
    tickets: input.tickets,
    nodes: input.nodes,
  });
  return { validation, assembled };
}
