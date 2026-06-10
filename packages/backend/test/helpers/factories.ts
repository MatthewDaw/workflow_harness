import type {
  Agent,
  Idea,
  IdeaSource,
  Project,
  SessionProjection,
  Skill,
  UnassignedEntry,
} from '@harness/shared';
import { orgScope } from '@harness/shared';

/**
 * Record factories for the org-catalog tests. Everything defaults to the
 * canonical test principal/org (`matt` / `acme`); pass overrides for the rest.
 */

export const MATT = 'matt';
export const ORG = 'acme';
export const SCOPE = orgScope(ORG);

export function makeSkill(name: string, body = '', over: Partial<Skill> = {}): Skill {
  return {
    name,
    scope: SCOPE,
    kind: 'skill',
    description: '',
    source: 'local',
    members: [],
    body,
    ...over,
  };
}

export function makeBundle(name: string, members: string[], over: Partial<Skill> = {}): Skill {
  return { name, scope: SCOPE, kind: 'bundle', description: '', source: 'local', members, ...over };
}

export function makeAgent(name: string, skills: string[] = [], over: Partial<Agent> = {}): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills, tools: [], ...over };
}

export function makeAgentBundle(
  name: string,
  members: string[],
  over: Partial<Agent> = {},
): Agent {
  return {
    name,
    scope: SCOPE,
    kind: 'bundle',
    model: '',
    prompt: '',
    skills: [],
    tools: [],
    members,
    ...over,
  };
}

export function makeProject(id: string, owner: string, over: Partial<Project> = {}): Project {
  return {
    id,
    name: id,
    repo: `gh/acme/${id}`,
    ownerUserId: owner,
    liveSessionCount: 0,
    enabledSkills: [],
    enabledAgents: [],
    enabledMcpServers: [],
    ...over,
  };
}

export function makeSessionProjection(
  id: string,
  projectId: string,
  owner: string,
  status: SessionProjection['status'],
  lastEventAt = 1,
): SessionProjection {
  return {
    sessionId: id,
    projectId,
    name: id,
    host: 'h',
    ownerUserId: owner,
    status,
    tokens: 0,
    startedAt: 1,
    lastEventAt,
    maxSeq: 0,
  };
}

// A monotonically-increasing seq shared by every fabricated source in a file.
let seq = 0;

export function makeSource(sessionId: string): IdeaSource {
  return { sessionId, segmentId: `${sessionId}-seg`, seq: seq++, snippet: '' };
}

/**
 * An idea with `sessions` DISTINCT sessions. `extraSegmentsOnFirst` extra
 * sources reuse the FIRST session id, so they must NOT raise the corroboration
 * count (the unit is the distinct session, deduped across segments).
 */
export function makeIdea(
  ideaId: string,
  opts: {
    sessions: number;
    status?: Idea['status'];
    org?: string;
    skillBaseName?: string;
    updatedAt?: number;
    extraSegmentsOnFirst?: number;
    foldedIntoRev?: number;
  },
): Idea {
  const sources: IdeaSource[] = [];
  for (let i = 0; i < opts.sessions; i++) sources.push(makeSource(`${ideaId}-sess-${i}`));
  for (let i = 0; i < (opts.extraSegmentsOnFirst ?? 0); i++) {
    // Reuse the first session id with a fresh segment — same session, more segments.
    sources.push({
      sessionId: `${ideaId}-sess-0`,
      segmentId: `extra-${i}`,
      seq: seq++,
      snippet: '',
    });
  }
  return {
    ideaId,
    skillBaseName: opts.skillBaseName ?? 'reconcile',
    org: opts.org ?? ORG,
    text: `lesson ${ideaId}`,
    sources,
    status: opts.status ?? 'open',
    ...(opts.foldedIntoRev !== undefined ? { foldedIntoRev: opts.foldedIntoRev } : {}),
    corroborationVersion: 0,
    createdAt: 1,
    updatedAt: opts.updatedAt ?? 1,
  };
}

export function makeBinEntry(
  entryId: string,
  opts: { sessions: number; org?: string; updatedAt?: number; extraSegmentsOnFirst?: number },
): UnassignedEntry {
  const sources: IdeaSource[] = [];
  for (let i = 0; i < opts.sessions; i++) sources.push(makeSource(`${entryId}-sess-${i}`));
  for (let i = 0; i < (opts.extraSegmentsOnFirst ?? 0); i++) {
    sources.push({
      sessionId: `${entryId}-sess-0`,
      segmentId: `extra-${i}`,
      seq: seq++,
      snippet: '',
    });
  }
  return {
    entryId,
    org: opts.org ?? ORG,
    text: `topic ${entryId}`,
    sources,
    createdAt: 1,
    updatedAt: opts.updatedAt ?? 1,
  };
}
