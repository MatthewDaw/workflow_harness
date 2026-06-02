import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import type { AgentProposal, ScoredSession } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Embedder } from './embed.js';
import { searchSimilarSessions, type SearchDeps } from './search.js';

/**
 * Forge agent proposal (U27, KTD7/KTD11).
 *
 * Given a description, find similar past sessions (search.ts), aggregate the
 * skills/tools those sessions used by frequency, and draft an agent. The
 * drafting LLM call sits behind an injectable `AgentDrafter` so tests run
 * deterministically; the default talks to Bedrock (Claude).
 *
 * Frequency aggregation is the evidence: a skill/tool that appears in many of
 * the similar sessions is high-confidence; one that appears in only a single
 * session is flagged `lowConfidence` so the editor (U24) can highlight it.
 */

export interface FrequencyEntry {
  name: string;
  count: number;
}

/**
 * Count how often each skill/tool appears across the similar sessions, ranked
 * most-frequent first. A name that appears in one session counts once even if
 * the session lists it twice (sessions are de-duped per name).
 */
export function aggregateFrequencies(
  sessions: ScoredSession[],
  pick: (s: ScoredSession) => string[],
): FrequencyEntry[] {
  const counts = new Map<string, number>();
  for (const s of sessions) {
    const seen = new Set(pick(s));
    for (const name of seen) counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

/**
 * Split aggregated names into the confident set and the low-confidence set.
 * Names meeting `minCount` (default 2 — seen in at least two sessions) are
 * confident; the rest are flagged.
 */
export function splitConfidence(
  freqs: FrequencyEntry[],
  minCount = 2,
): { confident: string[]; low: string[] } {
  const confident: string[] = [];
  const low: string[] = [];
  for (const f of freqs) {
    if (f.count >= minCount) confident.push(f.name);
    else low.push(f.name);
  }
  return { confident, low };
}

/** Drafts an agent given the mined evidence. Injectable (Bedrock by default). */
export interface AgentDrafter {
  draft(input: {
    description: string;
    skills: FrequencyEntry[];
    tools: FrequencyEntry[];
    sessions: ScoredSession[];
  }): Promise<{ name: string; model: string; prompt: string }>;
}

/** Bedrock-backed agent drafter (Claude via the Messages API on Bedrock). */
export class BedrockAgentDrafter implements AgentDrafter {
  constructor(
    private readonly client: BedrockRuntimeClient,
    private readonly modelId: string = process.env.BEDROCK_TEXT_MODEL ??
      'anthropic.claude-3-5-sonnet-20240620-v1:0',
  ) {}

  async draft(input: {
    description: string;
    skills: FrequencyEntry[];
    tools: FrequencyEntry[];
    sessions: ScoredSession[];
  }): Promise<{ name: string; model: string; prompt: string }> {
    const res = await this.client.send(
      new InvokeModelCommand({
        modelId: this.modelId,
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify({
          anthropic_version: 'bedrock-2023-05-31',
          max_tokens: 512,
          messages: [
            {
              role: 'user',
              content:
                `Draft an agent for: "${input.description}". ` +
                `Frequent skills: ${input.skills.map((s) => s.name).join(', ')}. ` +
                `Frequent tools: ${input.tools.map((t) => t.name).join(', ')}. ` +
                `Reply as JSON {"name","model","prompt"}.`,
            },
          ],
        }),
      }),
    );
    const decoded = JSON.parse(new TextDecoder().decode(res.body)) as {
      content?: { text?: string }[];
    };
    const text = decoded.content?.[0]?.text ?? '{}';
    try {
      const parsed = JSON.parse(text) as { name?: string; model?: string; prompt?: string };
      return {
        name: parsed.name ?? 'proposed-agent',
        model: parsed.model ?? this.modelId,
        prompt: parsed.prompt ?? '',
      };
    } catch {
      return { name: 'proposed-agent', model: this.modelId, prompt: text };
    }
  }
}

export interface ProposeDeps extends SearchDeps {
  repo: Repo;
  embedder: Embedder;
  drafter: AgentDrafter;
  /** Minimum sessions a skill/tool must appear in to count as confident. */
  minConfidentCount?: number;
}

/**
 * The full Forge proposal flow: search similar sessions, aggregate skills/tools
 * by frequency, draft an agent, and flag low-confidence additions.
 *
 * With no similar history the result is `insufficientHistory: true` and a blank
 * but editable draft (the plan's "not enough history" edge case), never a throw.
 */
export async function proposeAgent(
  opts: { userId: string; description: string; k?: number },
  deps: ProposeDeps,
): Promise<AgentProposal> {
  const sessions = await searchSimilarSessions(opts, deps);

  if (sessions.length === 0) {
    return {
      name: 'new-agent',
      model: process.env.BEDROCK_TEXT_MODEL ?? 'anthropic.claude-3-5-sonnet-20240620-v1:0',
      prompt: '',
      skills: [],
      tools: [],
      lowConfidence: [],
      evidence: [],
      insufficientHistory: true,
    };
  }

  const skillFreqs = aggregateFrequencies(sessions, (s) => s.skills);
  const toolFreqs = aggregateFrequencies(sessions, (s) => s.tools);
  const minCount = deps.minConfidentCount ?? 2;
  const skills = splitConfidence(skillFreqs, minCount);
  const tools = splitConfidence(toolFreqs, minCount);

  const drafted = await deps.drafter.draft({
    description: opts.description,
    skills: skillFreqs,
    tools: toolFreqs,
    sessions,
  });

  return {
    name: drafted.name,
    model: drafted.model,
    prompt: drafted.prompt,
    skills: skills.confident,
    tools: tools.confident,
    lowConfidence: [...skills.low, ...tools.low],
    evidence: sessions,
    insufficientHistory: false,
  };
}
