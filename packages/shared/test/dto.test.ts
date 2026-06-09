import { describe, it, expect } from 'vitest';
import {
  definitionOfDoneSchema,
  orgConfigSchema,
  DEFAULT_DEFINITION_OF_DONE,
  CONTROL_ACTIONS,
  controlActionSchema,
  controlFrameSchema,
  mcpServerSchema,
  MCP_TRANSPORTS,
  projectSchema,
  agentSchema,
  skillSchema,
  workflowSchema,
  truePointerSchema,
  variantIdFor,
  normalizeEnabledEntry,
  enabledEntryName,
  enabledEntryVariant,
} from '../src/dto.js';

const orgScope = { tier: 'org', id: 'acme' } as const;

/**
 * The org-wide Definition of Done (plan-mapping feature 1). Advisory config that
 * `/update-progress` reports against; it has a sensible floor as defaults.
 */
describe('definitionOfDoneSchema', () => {
  it('applies the org-wide floor as defaults on an empty object', () => {
    const dod = definitionOfDoneSchema.parse({});
    expect(dod.requiresUnitTests).toBe(true); // floor: unit tests
    expect(dod.requiresProdE2E).toBe(false); // tighten-only opt-in
    expect(dod.notes).toBeUndefined();
  });

  it('round-trips a tightened DoD (prod-E2E + notes)', () => {
    const dod = definitionOfDoneSchema.parse({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'Run npm run e2e:prod against the deployed env.',
    });
    expect(dod).toEqual({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'Run npm run e2e:prod against the deployed env.',
    });
  });

  it('rejects a non-boolean flag', () => {
    expect(definitionOfDoneSchema.safeParse({ requiresUnitTests: 'yes' }).success).toBe(false);
  });

  it('DEFAULT_DEFINITION_OF_DONE matches the parsed defaults', () => {
    expect(definitionOfDoneSchema.parse({})).toEqual(DEFAULT_DEFINITION_OF_DONE);
  });
});

/**
 * Control actions are the shared source of truth for HQ → daemon steering: the
 * web sender, the REST/WS control routes, and the wrapper receiver all key off
 * this enum. `shutdown`/`kill` were added so a live session can be terminated
 * from HQ (graceful, with a force fallback).
 */
describe('controlActionSchema', () => {
  it('includes inject/pause/interrupt and the terminate actions shutdown/kill', () => {
    expect(CONTROL_ACTIONS).toEqual(['inject', 'pause', 'interrupt', 'shutdown', 'kill']);
    for (const a of ['shutdown', 'kill'] as const) {
      expect(controlActionSchema.safeParse(a).success).toBe(true);
    }
  });

  it('rejects an unknown action', () => {
    expect(controlActionSchema.safeParse('restart').success).toBe(false);
  });

  it('parses a shutdown control frame with an empty payload default', () => {
    const frame = controlFrameSchema.parse({ sessionId: 's1', action: 'shutdown' });
    expect(frame).toEqual({ sessionId: 's1', action: 'shutdown', payload: {} });
  });
});

/**
 * MCP servers are the structured catalog record (a `transport` discriminated
 * union), the third pillar alongside skills/agents. stdio defaults args/env;
 * http/sse require a valid url; an unknown transport fails the union.
 */
describe('mcpServerSchema', () => {
  it('exposes the supported transports', () => {
    expect(MCP_TRANSPORTS).toEqual(['stdio', 'http', 'sse']);
  });

  it('parses a valid stdio server and defaults args/env when omitted', () => {
    const server = mcpServerSchema.parse({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
    });
    expect(server).toEqual({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
      args: [],
      env: {},
    });
  });

  it('round-trips a stdio server with args + env', () => {
    const server = mcpServerSchema.parse({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
      env: { TOKEN: 'secret' },
    });
    expect(server).toMatchObject({
      transport: 'stdio',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
      env: { TOKEN: 'secret' },
    });
  });

  it('parses a valid http server and defaults headers when omitted', () => {
    const server = mcpServerSchema.parse({
      name: 'remote',
      scope: orgScope,
      transport: 'http',
      url: 'https://mcp.example.com/sse',
    });
    expect(server).toEqual({
      name: 'remote',
      scope: orgScope,
      transport: 'http',
      url: 'https://mcp.example.com/sse',
      headers: {},
    });
  });

  it('rejects an http server with a non-URL url', () => {
    const result = mcpServerSchema.safeParse({
      name: 'remote',
      scope: orgScope,
      transport: 'http',
      url: 'not-a-url',
    });
    expect(result.success).toBe(false);
  });

  it('rejects an unknown transport', () => {
    const result = mcpServerSchema.safeParse({
      name: 'bad',
      scope: orgScope,
      transport: 'websocket',
      url: 'https://mcp.example.com',
    });
    expect(result.success).toBe(false);
  });

  it('rejects a stdio server missing its command', () => {
    expect(
      mcpServerSchema.safeParse({ name: 'fs', scope: orgScope, transport: 'stdio' }).success,
    ).toBe(false);
  });
});

/**
 * Back-compat: project/agent records written before MCP servers existed lack the
 * new attachment arrays; `.default([])` keeps them valid and fills the field.
 */
describe('MCP server attachment back-compat', () => {
  it('defaults enabledMcpServers to [] for a legacy project', () => {
    const project = projectSchema.parse({
      id: 'p1',
      name: 'Weekly Compass',
      repo: 'gh/acme/weekly-compass',
      ownerUserId: 'u1',
    });
    expect(project.enabledMcpServers).toEqual([]);
  });

  it('defaults mcpServers to [] for a legacy agent', () => {
    const agent = agentSchema.parse({
      name: 'planner',
      scope: orgScope,
      model: 'claude-opus-4-8',
    });
    expect(agent.mcpServers).toEqual([]);
  });

  it("defaults kind to 'agent' and members to [] for a legacy agent", () => {
    const agent = agentSchema.parse({ name: 'planner', scope: orgScope, model: 'opus' });
    expect(agent.kind).toBe('agent');
    expect(agent.members).toEqual([]);
  });

  it('accepts an agent bundle (kind:bundle) without a model', () => {
    const bundle = agentSchema.parse({
      name: 'research-subagents',
      scope: orgScope,
      kind: 'bundle',
      members: ['codebase-locator', 'web-search-researcher'],
    });
    expect(bundle.kind).toBe('bundle');
    expect(bundle.model).toBe('');
    expect(bundle.members).toEqual(['codebase-locator', 'web-search-researcher']);
  });

  it('rejects a runnable agent (kind:agent) with an empty model', () => {
    const res = agentSchema.safeParse({ name: 'planner', scope: orgScope, model: '' });
    expect(res.success).toBe(false);
  });
});

/**
 * VERSIONING MODEL (KTD6). The variant/revision fields are MIXED INTO skill /
 * agent / mcpServer and are all optional/back-compat: a legacy record (no
 * version fields) still validates and gains NO fabricated fields.
 */
describe('versioning fields (back-compat)', () => {
  it('a legacy skill (no version fields) still validates and stays minimal', () => {
    const skill = skillSchema.parse({
      name: 'reconcile',
      scope: orgScope,
      kind: 'skill',
    });
    // No version field is fabricated on a legacy record.
    expect(skill.version).toBeUndefined();
    expect(skill.variantId).toBeUndefined();
    expect(skill.baseName).toBeUndefined();
    expect(skill.repoId).toBeUndefined();
    expect(skill.files).toBeUndefined();
  });

  it('round-trips a forked skill variant with version fields + whole-dir files', () => {
    const skill = skillSchema.parse({
      name: 'reconcile',
      scope: orgScope,
      kind: 'skill',
      baseName: 'reconcile',
      variantId: 'reconcile#R#weekly-compass#U#matt',
      repoId: 'weekly-compass',
      authorUserId: 'matt',
      version: 3,
      createdAt: 1717200000000,
      files: { 'SKILL.md': '# Reconcile', 'scripts/run.sh': 'echo hi' },
    });
    expect(skill.version).toBe(3);
    expect(skill.variantId).toBe('reconcile#R#weekly-compass#U#matt');
    expect(skill.files).toEqual({ 'SKILL.md': '# Reconcile', 'scripts/run.sh': 'echo hi' });
  });

  it('a legacy agent / mcp server validates without version fields', () => {
    const agent = agentSchema.parse({ name: 'planner', scope: orgScope, model: 'opus' });
    expect(agent.version).toBeUndefined();
    const mcp = mcpServerSchema.parse({
      name: 'fs',
      scope: orgScope,
      transport: 'stdio',
      command: 'npx',
    });
    expect((mcp as { version?: number }).version).toBeUndefined();
  });

  it('rejects a non-positive version', () => {
    expect(
      skillSchema.safeParse({ name: 's', scope: orgScope, kind: 'skill', version: 0 }).success,
    ).toBe(false);
  });
});

describe('variantIdFor', () => {
  it('returns the baseName itself for the base variant (no repo + no user)', () => {
    expect(variantIdFor('reconcile')).toBe('reconcile');
    expect(variantIdFor('reconcile', undefined, undefined)).toBe('reconcile');
  });

  it('appends repo + user for a fork', () => {
    expect(variantIdFor('reconcile', 'weekly-compass', 'matt')).toBe(
      'reconcile#R#weekly-compass#U#matt',
    );
  });
});

describe('truePointerSchema', () => {
  it('parses a pointer with an explicit rev', () => {
    expect(truePointerSchema.parse({ baseName: 's', variantId: 's', rev: 2 })).toEqual({
      baseName: 's',
      variantId: 's',
      rev: 2,
    });
  });

  it('allows an absent rev (means latest)', () => {
    const p = truePointerSchema.parse({ baseName: 's', variantId: 's' });
    expect(p.rev).toBeUndefined();
  });
});

describe('enabled-set entry pins (U-Ver-Pin, back-compat)', () => {
  it('normalizes a bare-string entry (no variant pin)', () => {
    expect(normalizeEnabledEntry('reconcile')).toEqual({ name: 'reconcile' });
    expect(enabledEntryName('reconcile')).toBe('reconcile');
    expect(enabledEntryVariant('reconcile')).toBeUndefined();
  });

  it('normalizes an object entry carrying a variantId', () => {
    const entry = { name: 'reconcile', variantId: 'reconcile#R#r#U#u' };
    expect(normalizeEnabledEntry(entry)).toEqual(entry);
    expect(enabledEntryName(entry)).toBe('reconcile');
    expect(enabledEntryVariant(entry)).toBe('reconcile#R#r#U#u');
  });
});

/**
 * A workflow is a DAG of catalog agents: each node references an agent and
 * carries its own `dependsOn` edges. The `superRefine` enforces unique node ids,
 * resolvable `dependsOn`/`declaredBy` refs, and acyclicity of the dependsOn graph
 * (`declaredBy` is a CONTROL edge, excluded from the cycle check). It defaults
 * `kind` to `'workflow'`, node `label`/`prompt` to '' and `dependsOn` to [].
 */
describe('workflowSchema superRefine (DAG validators)', () => {
  const wfNode = (id: string, agent: string, dependsOn: string[] = []) => ({
    id,
    agent,
    dependsOn,
  });
  const wf = (nodes: ReturnType<typeof wfNode>[]) => ({
    name: 'pipeline',
    scope: orgScope,
    nodes,
  });

  it('ACCEPTS a valid DAG and fills node defaults', () => {
    const parsed = workflowSchema.parse(
      wf([wfNode('build', 'builder'), wfNode('test', 'tester', ['build'])]),
    );
    expect(parsed.kind).toBe('workflow');
    expect(parsed.nodes.map((n) => n.id)).toEqual(['build', 'test']);
    expect(parsed.nodes[0]).toMatchObject({ label: '', prompt: '', dependsOn: [] });
  });

  it('REJECTS a cyclic dependsOn graph', () => {
    const res = workflowSchema.safeParse(
      wf([wfNode('a', 'x', ['b']), wfNode('b', 'y', ['a'])]),
    );
    expect(res.success).toBe(false);
    if (!res.success) {
      expect(res.error.issues.some((i) => /cycle/.test(i.message))).toBe(true);
    }
  });

  it('REJECTS a self-dependency cycle', () => {
    const res = workflowSchema.safeParse(wf([wfNode('a', 'x', ['a'])]));
    expect(res.success).toBe(false);
  });

  it('REJECTS a dangling dependsOn reference', () => {
    const res = workflowSchema.safeParse(wf([wfNode('build', 'builder', ['ghost'])]));
    expect(res.success).toBe(false);
    if (!res.success) {
      expect(res.error.issues.some((i) => /unknown node id 'ghost'/.test(i.message))).toBe(true);
    }
  });

  it('REJECTS a dangling declaredBy reference', () => {
    const res = workflowSchema.safeParse({
      name: 'pipeline',
      scope: orgScope,
      nodes: [
        { id: 'gen', agent: 'writer', rerun: { mode: 'declared-by', declaredBy: 'ghost' } },
      ],
    });
    expect(res.success).toBe(false);
    if (!res.success) {
      expect(
        res.error.issues.some((i) => /declaredBy references unknown node id/.test(i.message)),
      ).toBe(true);
    }
  });

  it('REJECTS duplicate node ids', () => {
    const res = workflowSchema.safeParse(wf([wfNode('a', 'x'), wfNode('a', 'y')]));
    expect(res.success).toBe(false);
    if (!res.success) {
      expect(res.error.issues.some((i) => /duplicate node id 'a'/.test(i.message))).toBe(true);
    }
  });

  it('ACCEPTS a declared-by control edge that would be a cycle as a DAG edge', () => {
    // gen depends on nothing; its checker `chk` depends on gen — the declaredBy
    // edge gen<-chk is a control edge, excluded from the acyclicity check.
    const parsed = workflowSchema.parse({
      name: 'gen-check',
      scope: orgScope,
      nodes: [
        { id: 'gen', agent: 'writer', rerun: { mode: 'declared-by', declaredBy: 'chk' } },
        { id: 'chk', agent: 'reviewer', dependsOn: ['gen'] },
      ],
    });
    expect(parsed.nodes.map((n) => n.id)).toEqual(['gen', 'chk']);
  });
});

describe('orgConfigSchema', () => {
  it('accepts an empty config (dod optional)', () => {
    expect(orgConfigSchema.parse({})).toEqual({});
  });

  it('embeds a Definition of Done', () => {
    const cfg = orgConfigSchema.parse({ dod: { requiresProdE2E: true } });
    expect(cfg.dod).toEqual({ requiresUnitTests: true, requiresProdE2E: true });
  });
});
